"""PLAYPEN: a support agent whose generated code can act only through calls a policy decides.

The agent works an online store's support queue by writing Python and running it with Code
Mode. The script runs in Monty — no filesystem, no network, no imports — and its only way to do
anything is to call one of the store's host functions (``examples/playpen/store.py``). Each of
those calls traps out of the sandbox into this workflow, where Playpen decides it against the
Dogwood policy in ``policies/support/policy.dw`` and everything the session has done so far:

* a call the policy permits runs;
* a call it forbids does not run, and its ``await`` raises ``PermissionError`` with the rule's
  reason, which the model reads and reports;
* a call denied only by a rule annotated ``@on_deny("escalate")`` waits for a person to approve
  it in the console.

Two ways to drive it: chat with the model (``ask``), or play one of the demo's fixed scripts
(``run_scripted_beat``) through the same policed tool with no model in the loop.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from pydantic import BaseModel
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from google.genai._interactions.types import FunctionCallStep
    from google.genai._interactions.types.function_result_step_param import (
        FunctionResultStepParam,
    )

    from temporal_agent_harness.ai_sdks.google_genai_plugin import (
        InteractionConversation,
        function_param,
        google_genai_client,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MidTurn,
        TextMessage,
        TextReply,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from temporal_agent_harness.playpen import DogwoodPolicy

    from . import scripted_beats, store


TASK_QUEUE = "playpen-support-agent"
MODEL = "gemini-3.8-flash"
CODE_TOOL_NAME = "run_support_code"
POLICY = "support"

SYSTEM_INSTRUCTION = """\
You are a support agent for Hearth & Home, an online homewares store. You work the support \
queue: read tickets, look up customers and orders, issue refunds, and email customers. You \
don't have these abilities directly. Instead you write small async Python scripts and run them \
with the `run_support_code` tool, which exposes the store's operations as async host functions. \
The tool's description gives their exact signatures and result shapes.

How to work:
- Do what customers ask when it is reasonable. Refund damaged, defective or wrong items in full, \
and send the emails a ticket asks for.
- Look up an order before refunding it, so you refund the right amount.
- Run independent host calls concurrently with `asyncio.gather`. You can work several tickets \
in one script.
- Some host calls may be refused by the store's policy: the `await` raises `PermissionError` \
with the reason. Catch it, carry on with the rest of the work, and tell the user plainly what \
was refused and why. Never try to get around a refusal.
- After a script runs, summarize what happened in plain prose: refunds issued, emails sent, \
anything refused or waiting on approval."""


class ScriptedBeat(BaseModel):
    """Which of the demo's fixed scripts to run."""

    beat: scripted_beats.Beat


@agent.defn(name="PlaypenSupportAgent")
class PlaypenSupportAgentWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        # The policy engine. Its evaluator decides every host call; its observer adds each
        # completed call, with its result, to the trace later calls are decided against.
        self._policy = DogwoodPolicy(POLICY)
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # The run-code tool is pre-approved: writing a script does nothing by itself, and
            # each host call the script makes is decided on its own.
            approval_policy_default=DogwoodPolicy.approval_policy(pre_approved=[CODE_TOOL_NAME]),
            auto_approval_criteria_default=DogwoodPolicy.criteria(),
            auto_mode_evaluator=self._policy.evaluator,
        )
        self._code_tool = agent.code_mode_tool(
            store.STORE_TOOLS,
            name=CODE_TOOL_NAME,
            on_host_call_result=self._policy.record_host_call_result,
        )
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=3)),
            runner=self._runner,
        )
        # The whole conversation, sent as the input of every model call (see
        # InteractionConversation for why it is not chained by interaction id).
        self._conversation = InteractionConversation()

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the support agent to work on the queue, in plain text: "handle ticket T-1",
        "work through the open tickets". It writes and runs scripts against the store, under
        the store's policy, and replies with what it did."""
        return TextReply(text=await self._chat_turn(message.text))

    @agent.accepts(mid_turn=MidTurn.ENQUEUE, model_callable=False)
    async def run_scripted_beat(self, message: ScriptedBeat) -> TextReply:
        """Run one of the demo's fixed scripts through the same policed tool, with no model:
        `ticket_t1` handles the ticket that asks for mail to an unverified address;
        `work_queue` refunds the whole queue at once, duplicate ticket included."""
        script = scripted_beats.SCRIPTS[message.beat]
        output = await self._runner.run_tool(
            f"beat-{message.beat}-{workflow.uuid4().hex[:8]}", self._code_tool, script=script
        )
        return TextReply(
            text=f"Ran the `{message.beat}` script:\n\n```python\n{script}```\n\n"
            f"Result:\n\n```\n{output}\n```"
        )

    # ------------------------------------------------------------------ chat loop

    async def _chat_turn(self, user_text: str) -> str:
        """Stream the model, run any ``run_support_code`` calls, feed the results back, and
        loop until the model replies with no further calls."""
        tools = [function_param(self._code_tool)]
        self._conversation.add_user_text(user_text)
        while True:
            stream = await self._gemini.interactions.create(
                model=MODEL,
                input=self._conversation.steps,
                system_instruction=SYSTEM_INSTRUCTION,
                tools=tools,
                stream=True,
            )
            reply = await self._conversation.read_reply(stream)
            if not reply.function_calls:
                return reply.text
            self._conversation.add_function_results(
                await asyncio.gather(*(self._run_one_tool(c) for c in reply.function_calls))
            )

    async def _run_one_tool(self, call: FunctionCallStep) -> FunctionResultStepParam:
        try:
            if call.name != CODE_TOOL_NAME:
                raise ValueError(f"unknown tool: {call.name!r}")
            result = await self._runner.run_tool(call.id, self._code_tool, **call.arguments)
            response: FunctionResultStepParam = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": str(result),
            }
        except Exception as e:
            response = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": str(e),
                "is_error": True,
            }
        return response
