"""A conversational agent with long-term memory kept in a directory on the worker's disk.

The agent's only tool is a Code Mode tool with no host functions and two mounts:

- ``/skills``, read-only: an :class:`~temporal_agent_harness.harness.agent.InMemoryFileSystem`
  seeded by the ``load_skills`` activity, holding the ``memory`` skill. The skill says how
  memory is laid out and how to recall, save and forget.
- ``/memory``, writable: :class:`~.local_disk.LocalDisk`, an ``ActivityFileSystem`` whose every
  operation is an activity on the directory named by the session's init data
  (``MemoryConfig``), inside this example's ``memories/`` folder.

Memory is not in the workflow: it lives on disk, so it outlasts the session, and every session
started with the same ``memory_dir`` reads and writes the same memories. The system prompt only
tells the model when to use the skill; what memory is and how to use it lives in the skill.

Both mounts are declared on the class with ``agent.vfs_mount``, so the console's files panel
shows them, and bound in ``@agent.init``, where the session's seed and config are known.
``/skills`` is a ``FileTree``, so its files, contents included, are agent state. ``/memory`` is a
``FileIndex``: paths and sizes only, with a file's contents read from disk when someone opens it
in the panel.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import PurePosixPath

from pydantic import BaseModel, Field
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
    from .local_disk import LocalDisk, LocalDiskConfig


TASK_QUEUE = "code-mode-memory"
MODEL = "gemini-3.8-flash"

SYSTEM_INSTRUCTION = """\
You are a helpful assistant with a long-term memory of the user. You work by writing short \
Python scripts and running them with the `run_code` tool, which gives your scripts a filesystem.

Your memory is managed by the `memory` skill at `/skills/memory/SKILL.md`. Use that skill:
- whenever the user asks you to remember, update or forget something;
- whenever the user asks something that may be in memory: anything about themselves, their \
preferences, plans, people or projects, or something they told you before.

Before using memory in a conversation, FIRST run a script that reads \
`/skills/memory/SKILL.md`, then follow it. Reply in plain prose."""


class MemoryConfig(BaseModel):
    """Where this session's memory lives."""

    memory_dir: PurePosixPath = Field(
        description=(
            "Directory holding this session's memory, relative to the example's memories/ "
            "folder (e.g. 'alice'), created if missing. Sessions given the same directory "
            "share their memories."
        ),
    )


@agent.defn(name="CodeModeMemoryAgent")
class CodeModeMemoryAgentWorkflow:
    skills = agent.vfs_mount(
        "/skills",
        agent.InMemoryFileSystem,
        read_only=True,
        description="Skills, one folder each; start with its SKILL.md.",
    )
    memory = agent.vfs_mount(
        "/memory",
        LocalDisk,
        description=(
            "Your long-term memory of the user, kept across sessions. Read and "
            "write it only as /skills/memory/SKILL.md describes."
        ),
    )

    @agent.init
    def __init__(self, config: AgentConfig, data: MemoryConfig) -> None:
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
                self.skills.bind(seed=self._load_skills),
                self.memory.bind(LocalDiskConfig(directory=data.memory_dir)),
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
        """Chat with the assistant. Tell it something to remember, or ask about something you
        told it before, in this session or another one sharing its memory."""
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
