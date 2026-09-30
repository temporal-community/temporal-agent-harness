"""DagBuilderAgent: an agent that writes a flow of agents as Python, and then runs it.

A *flow* is a Python script that wires step agents into a DAG: ``await`` one step after another
to run them in sequence, ``asyncio.gather`` them to run them in parallel, and pass what one step
returned into the next one's prompt. Its one host function, :func:`run_agent`, starts a
:class:`~.step_agent.DagStepAgentWorkflow` as a subagent, configured by the script for that step
alone (a name, a system prompt, a set of tools and a model), gives it its task, and returns its
reply.

The agent has two handlers:

* ``generate`` — an OpenAI Agents SDK turn that writes or revises the flow for the user's request.
  The script lives in the user's editor, not here, so the model reads and changes it through the
  ``read_code`` / ``write_code`` / ``edit_code`` callback tools (``editor_tools.py``), which the page
  fulfils, drawing each edit as it lands. The model's prompt carries the exact host-function
  contract a script runs against, taken from the Code Mode tool itself, and ``check_code``
  type-checks the script against that contract without running it, so the model does not reply
  until the script passes.
* ``execute`` — runs a script in Code Mode over ``run_agent``, with no model in the loop, like the Monty
  example's ``run_script``. The script is type-checked against ``run_agent``'s signature before it
  runs, so an unknown tool name or a missing field comes back as an error to fix.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import ModelSettings, Runner, TResponseInputItem

    from temporal_agent_harness.ai_sdks.openai_agents_harness import as_openai_agent_tool
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent import Injected
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MidTurn,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from . import step_tools
    from .editor_tools import EDITOR_TOOLS, export_code
    from .models import AgentStep, AgentStepResult, RunFlow, StepSpec
    from .step_agent import WORKFLOW_TYPE as STEP_WORKFLOW_TYPE


BUILDER_MODEL = "gpt-6-luna"

# The subagent key every step is started under; it prefixes each step's workflow id.
STEP_KEY = "step"


@agent.tool_defn(inherently_safe=True)
async def run_agent(runner: Injected[AgentWorkflowRunner], step: AgentStep) -> AgentStepResult:
    """Start a fresh agent for one step of the flow, give it `prompt`, and return its reply.

    The agent is set up for this step alone: `name` labels it, `instructions` is its system
    prompt, `tools` is the subset of the step tools it may call, and `model` runs it. It is stopped
    once it replies, so every call is a new agent that knows only what its `prompt` tells it —
    put upstream steps' outputs into the prompt. Calls gathered with `asyncio.gather` run in
    parallel."""
    spec = StepSpec(
        name=step.name, instructions=step.instructions, tools=step.tools, model=step.model
    )
    # Steps run on the builder's own queue: one worker hosts both agents.
    handle = await runner.start_subagent(
        STEP_KEY, STEP_WORKFLOW_TYPE, workflow.info().task_queue, data=spec
    )
    try:
        reply = await runner.run_subagent_turn(handle, "ask", {"text": step.prompt})
    finally:
        await runner.stop_subagent(handle)
    return AgentStepResult(name=step.name, output=reply["text"])


@agent.tool_defn(inherently_safe=True)
async def check_code(
    runner: Injected[AgentWorkflowRunner], flow_tool: Injected[Callable[..., Awaitable[str]]]
) -> str:
    """Type-check the script in the editor against the flow contract, without running it.

    Reports every syntax and type error with its line: an unknown tool name, a missing
    `run_agent` field, a result key that doesn't exist. A script that passes will start when the
    user presses Run. Call it after your last edit, and again after fixing what it reports."""
    script = await runner.run_tool(str(workflow.uuid4()), export_code)
    if not script.strip():
        return "The editor is empty; write the flow first."
    report = await agent.code_mode_type_check(flow_tool, script)
    if report is None:
        return "No errors: the script type-checks and will start when the user presses Run."
    return f"The script has errors. Fix them with edit_code, then call check_code again.\n\n{report}"


BUILDER_INSTRUCTIONS = """\
You design *flows*: small Python scripts that wire AI agents into a DAG to get a job done. The \
user describes what they want; you write the flow, or change the one they have. You never run it \
— the user presses Run.

The script lives in the user's editor, and you can only reach it through these tools:
- `read_code` shows the script with line numbers. Read it before changing anything.
- `write_code` replaces the whole script: use it for a first draft, or when asked to start over.
- `edit_code` replaces one exact, unique piece of text. Use it for every change to an existing \
script, as several small, targeted edits rather than one big one — the user watches each edit \
land, so make each one a meaningful step.
- `check_code` type-checks the script without running it and reports every error with its line.

You can call several of these at once. The editor applies them one at a time, in the order you \
list them, so write each `edit_code` against the script as the edits before it leave it, and \
never let two edits in one batch overlap.

Never hand back a script that doesn't type-check. When you have finished editing, call \
`check_code`; if it reports errors, fix them with `edit_code` and call it again, until it \
passes. Only then reply. If something truly can't be fixed, say so and quote the error.

How a flow works: it calls the host function `run_agent` once per step. Each call starts a fresh \
agent configured just for that step — its own name, system prompt (`instructions`), tools and \
model — gives it `prompt`, and returns its reply. Sequence steps by awaiting them in order; run \
independent steps in parallel with `asyncio.gather`; feed a step's `["output"]` into later steps' \
prompts with f-strings. A step knows nothing but its instructions and prompt, so pass it \
everything it needs.

Write flows people enjoy reading:
- Start with a short docstring saying what the flow does and its shape (e.g. "3 scouts in \
parallel -> planner -> judge").
- Put each step's settings in a clearly named variable or helper, and keep instructions focused.
- Give a step only the tools it needs; a step with no tools just thinks and writes.
- Two to six steps is plenty. End by returning the final step's output (or a dict of outputs).
- Use real values from the user's request (cities, dates, names) and sensible assumptions \
otherwise.

The step tools a step can be given:
{catalog}

Below is the exact contract a flow runs under. The script is type-checked against it before it \
runs, so follow it exactly.

{contract}

When you're done editing, reply to the user in one to three sentences: what the flow does and \
its shape. Don't paste the code; they can see it."""


@agent.defn(name="DagBuilderAgent")
class DagBuilderAgentWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # The editor tools and run_agent are inherently safe (the user's own editor, and
            # simulated step tools), so nothing waits on a click.
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._run_flow = agent.code_mode_tool(
            [run_agent], name="run_flow", injections={"runner": self._runner}
        )
        self._instructions = BUILDER_INSTRUCTIONS.format(
            catalog=step_tools.catalog(), contract=self._run_flow.__doc__
        )
        self._conversation: list[TResponseInputItem] = []
        # What the last run printed and returned, until the next `generate` hears about it.
        self._unseen_run: str | None = None

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def generate(self, message: TextMessage) -> TextReply:
        """Describe the flow you want, or a change to the current one. The agent writes it into
        your editor, one edit at a time."""
        text = message.text
        if self._unseen_run is not None:
            text = f"(The user ran the flow. Its output:\n{self._unseen_run}\n)\n\n{text}"
            self._unseen_run = None
        sdk_agent = OpenAIAgent(
            name="Flow builder",
            instructions=self._instructions,
            model=BUILDER_MODEL,
            tools=[
                *(as_openai_agent_tool(self._runner, tool) for tool in EDITOR_TOOLS),
                as_openai_agent_tool(
                    self._runner,
                    check_code,
                    injections={"runner": self._runner, "flow_tool": self._run_flow},
                ),
            ],
            # Several edits may come in one response. The studio applies its editor callbacks
            # one at a time, in the order they reach the stream, so each edit lands on the
            # script the one before it left.
            model_settings=ModelSettings(parallel_tool_calls=True),
        )
        result = Runner.run_streamed(
            sdk_agent,
            input=[*self._conversation, {"role": "user", "content": text}],
            context=self._runner,
            max_turns=30,
        )
        async for _event in result.stream_events():
            pass
        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def execute(self, message: RunFlow) -> TextReply:
        """Run a flow script. Each `run_agent` call starts a step agent as a subagent; the reply is
        what the script printed and returned."""
        output = await self._runner.run_tool(
            str(workflow.uuid4()), self._run_flow, script=message.script
        )
        self._unseen_run = output
        return TextReply(text=output)
