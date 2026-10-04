"""A conversational agent whose Code Mode scripts work on a virtual filesystem.

The agent's only tool is a Code Mode tool with no host functions and two mounts: ``/skills``,
read-only, and ``/workspace``, where the agent writes what it produces. Both are
:class:`~temporal_agent_harness.harness.agent.InMemoryFileSystem` instances, so their files live
in the workflow. ``/skills`` is seeded by the ``load_skills`` activity before the first script
touches it; ``/workspace`` starts empty and keeps its files for the whole conversation.

``/workspace`` is tracked in the ``workspace`` state (opt-in, through ``state=``): its files live
in that state, and every change is published as a state patch the console shows. ``/skills`` is
left untracked, since nothing there changes after the seed.

``/skills`` holds a minimal sample of instruction-only skills from ``skills/``, as something for
the scripts to read: the model learns about them from the system prompt and the mount
descriptions, reads a ``SKILL.md`` when one fits the request, and follows it with ordinary file
reads and writes. Every file operation runs through the runner as an ``fs_*`` tool call, so it
shows up in the tool lifecycle like any tool.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

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
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .activities import load_skills


TASK_QUEUE = "code-mode-vfs"
MODEL = "gemini-3.8-flash"

SYSTEM_INSTRUCTION = """\
You are a helpful assistant with skills. A skill is a folder of instructions and supporting \
files under `/skills`. You work by writing short Python scripts and running them with the \
`run_code` tool, which gives your scripts a filesystem.

Available skills:
- `expense-report`: turn a list of expenses into a categorized expense report with totals.
- `meeting-notes`: turn a rough transcript or bullet dump into structured meeting notes.

When a request fits a skill, FIRST run a script that reads `/skills/<name>/SKILL.md`, then \
follow its instructions, reading any files it points to. Write what you produce under \
`/workspace`. Reply in plain prose once the work is done."""


@agent.defn(name="CodeModeVfsAgent")
class CodeModeVfsAgentWorkflow:
    # The /workspace files, as observable state: every file the agent writes is published as a
    # state patch, so the console's AGENT STATE pane shows the workspace as it changes.
    workspace = agent.state(agent.FileTree)

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Every file operation is a tool call; approving each one would be a click per read.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation = InteractionConversation()
        self._code_tool = agent.code_mode_tool(
            [],
            name="run_code",
            mounts=[
                agent.Mount(
                    "/skills",
                    agent.InMemoryFileSystem(seed=self._load_skills),
                    read_only=True,
                    description="Skills, one folder each; start with its SKILL.md.",
                ),
                agent.Mount(
                    "/workspace",
                    agent.InMemoryFileSystem(max_bytes=1_000_000, state=self.workspace),
                    description="Your scratch space; write everything you produce here.",
                ),
            ],
        )
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=3)),
            runner=self._runner,
        )

    async def _load_skills(self) -> dict[str, str]:
        return await workflow.execute_activity(
            load_skills, start_to_close_timeout=timedelta(seconds=30)
        )

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Ask for an expense report or meeting notes, pasting the
        expenses or transcript; it picks the matching skill, follows it, and writes the result
        to its workspace."""
        tools = [function_param(self._code_tool)]
        self._conversation.add_user_text(message.text)
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
                return TextReply(text=reply.text)
            self._conversation.add_function_results(
                await asyncio.gather(*(self._run_one_tool(fc) for fc in reply.function_calls))
            )

    async def _run_one_tool(self, call: FunctionCallStep) -> FunctionResultStepParam:
        try:
            if call.name != self._code_tool.__name__:
                raise ValueError(f"unknown tool: {call.name!r}")
            result = str(
                await self._runner.run_tool(
                    call.id, self._code_tool, injections=None, **call.arguments
                )
            )
            is_error = False
        except Exception as e:
            result, is_error = str(e), True
        response: FunctionResultStepParam = {
            "type": "function_result",
            "call_id": call.id,
            "name": call.name,
            "result": result,
        }
        if is_error:
            response["is_error"] = True
        if call.signature:
            response["signature"] = call.signature
        return response
