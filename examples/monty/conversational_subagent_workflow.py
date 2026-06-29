"""Conversational Monty agent that drives the script-runner as a SUBAGENT.

This is the subagent-flavoured twin of :class:`MontyChatAgentWorkflow`
(``conversational_workflow.py``). Both put a *model in the loop*: the user chats in plain
text, the model converses to gather what it needs, then writes its own Python script and runs
it in the Monty sandbox. The conversational front end — the OpenAI Agents SDK tool-calling
loop and the script-writing system prompt — is IDENTICAL.

The ONE difference is *where the script runs*. :class:`MontyChatAgentWorkflow` runs each
model-authored script inline, via a ``run_monty_script`` ``@agent.tool_defn`` backed by a
:class:`MontyHostDriver` held on the workflow itself. This agent instead drives the barebones
:class:`~.workflow.MontyDynamicAgentWorkflow` — whose sole ``@agent.accepts`` handler,
``run_script(RunScript) -> TextReply``, executes a script in the Monty sandbox — as a
**subagent**. The script-runner is wired with
``agent.subagent_toolset(MontyDynamicAgentWorkflow, key="monty", task_queue=TASK_QUEUE)``,
which generates three model-facing tools: ``start_monty`` (start an instance, returns a short
handle), ``monty_run_script`` (send a script to that instance and get its reply), and
``stop_monty`` (shut it down). So ``monty_run_script`` is a drop-in replacement for the inline
``run_monty_script`` tool — same capability, now across a real parent→subagent boundary.

Why this exists: it's the first real end-to-end exercise of the subagent toolset
(``docs/agents-as-subagents.md``). It validates the handle indirection, multiple turns per
subagent (the per-subagent FIFO gate + turn counter + stream-offset resume), and the
``run_subagent_turn`` activity against a live child workflow.

Approval stance: this agent runs under ``always_require_approvals`` (like
:class:`MontyChatAgentWorkflow`), so it **gates the subagent tools** — every ``start_monty`` /
``monty_run_script`` / ``stop_monty`` call escalates to a human. The script's host calls
(search/book flights & hotels) run *inside the child*, which keeps its own
``dangerously_skip_all`` policy, so — unlike the inline agent — those host calls are NOT gated
in the parent. (Forwarding a gating policy into the child is a possible follow-up.)
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner, TResponseInputItem

    from temporal_agent_harness.ai_sdks.openai_agents_plugin import as_openai_agent_tools
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        SlashCommand,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    # Reuse the script-writing contract verbatim from the inline agent — the rules the model
    # must follow to author a Monty script are identical; only the tool it calls differs.
    from .conversational_workflow import (
        MODEL_OPERATOR_COMMAND,
        SUPPORTED_MODELS,
        SET_MODEL_COMMAND,
        _SCRIPT_CONTRACT,
        _HarnessOpenAIRunHooks,
    )
    from .workflow import TASK_QUEUE, MontyDynamicAgentWorkflow


DEFAULT_MODEL = SUPPORTED_MODELS[0]

# The namespace for the wired script-runner subagent. Tool names are derived from it:
# start_monty / monty_run_script / stop_monty.
SUBAGENT_KEY = "monty"


SYSTEM_INSTRUCTION = f"""\
You are a friendly travel-booking assistant. You help users search and book flights and \
hotels and assemble trip itineraries. You don't have these abilities directly — instead you \
write small **async** Python scripts and run them in a Monty sandbox. Every script MUST be \
async: the host functions are coroutines you `await`, you run independent ones concurrently \
with `asyncio.gather`, and you wrap the body in `asyncio.run(main())` (full rules below).

You run scripts through a dedicated **script-runner subagent**, using these tools:
- `start_{SUBAGENT_KEY}`: start a script-runner and get back a short `subagent` handle. Call \
this ONCE at the start of the conversation, then reuse the same handle for every script.
- `{SUBAGENT_KEY}_run_script`: send a script to a running script-runner. Pass the handle from \
`start_{SUBAGENT_KEY}` as `subagent`, and the script in `message` (a RunScript object with a \
`script` field). The reply carries the script's printed output and final value.

{_SCRIPT_CONTRACT}

How to behave:
- Converse naturally. Ask brief clarifying questions when you're missing something essential \
(origin/destination, dates, traveler name) — don't interrogate; make reasonable assumptions \
and state them.
- Before running your first script, call `start_{SUBAGENT_KEY}` to get a handle. Keep using \
that one handle for the rest of the conversation; don't start a new script-runner per script.
- When you have enough to make progress, WRITE A SCRIPT and run it with \
`{SUBAGENT_KEY}_run_script`. Keep each script focused (search, or book, or summarize) so you \
can react to results.
- After a tool result, read it and reply to the user in plain, friendly prose — summarize \
options, prices, confirmations. You may run more scripts in follow-up turns.
- Never invent flight_ids/hotel_ids/confirmation codes — only use ones returned by a script."""


@workflow.defn(name="MontyChatSubagentAgent")
@agent.defn
class MontyChatSubagentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Gate the subagent tools: every start_monty / monty_run_script / stop_monty call
            # escalates to a human (same stance as the inline MontyChatAgent). The script's
            # host calls run inside the child, which has its own dangerously_skip_all policy.
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            operator_commands=[MODEL_OPERATOR_COMMAND],
            operator_command_handler=self._handle_operator_command,
        )
        self._model: str = DEFAULT_MODEL
        self._conversation: list[TResponseInputItem] = []
        # The model-facing tools: drive the barebones MontyDynamicAgent script-runner as a
        # subagent. Built statically from its @agent.accepts handlers — no child started here.
        # Yields start_monty / monty_run_script / stop_monty.
        self._tools = agent.subagent_toolset(
            MontyDynamicAgentWorkflow,
            key=SUBAGENT_KEY,
            task_queue=TASK_QUEUE,
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the travel assistant. Describe the trip you want (flights, hotels,
        dates, traveler name) in plain text; the assistant converses, writes Python scripts,
        and runs them against a simulated travel backend via a script-runner subagent, then
        replies with the results."""
        reply_text = await self._handle_chat_turn(message.text)
        return TextReply(text=reply_text)

    @agent.accepts
    async def slash(self, command: SlashCommand) -> TextReply:
        """Apply a slash command to this parent agent session."""
        reply = self._handle_operator_command(command)
        if reply is not None:
            return reply
        return TextReply(
            text=(
                f"Unknown Monty slash command: `{command.name}`. Try `/model`. "
                "Harness commands include `/approvals`, `/allow-tools`, and `/status`."
            )
        )

    def _handle_operator_command(self, command: SlashCommand) -> TextReply | None:
        if command.name == SET_MODEL_COMMAND:
            return self._set_model(command.arg)
        return None

    def _set_model(self, model: str | None) -> TextReply:
        if model is None or model not in SUPPORTED_MODELS:
            choices = ", ".join(f"`{model}`" for model in SUPPORTED_MODELS)
            return TextReply(text=f"Choose one of: {choices}.")
        self._model = model
        return TextReply(text=f"Model set to **{self._model}**.")

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, user_text: str) -> str:
        """Run one conversational turn with the OpenAI Agents SDK."""
        sdk_agent = OpenAIAgent(
            name="MontySubagent",
            instructions=SYSTEM_INSTRUCTION,
            model=self._model,
            tools=as_openai_agent_tools(
                self._runner,
                [
                    fn
                    for fn in self._tools
                    if fn.__name__ != f"stop_{SUBAGENT_KEY}"
                ],
            ),
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": user_text},
        ]
        result = await Runner.run(
            sdk_agent,
            input=input_items,
            hooks=_HarnessOpenAIRunHooks(self._runner, self._model),
        )
        self._conversation = result.to_input_list()
        return str(result.final_output)
