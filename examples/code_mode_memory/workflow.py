"""A conversational agent with long-term memory kept as an OKF knowledge bundle on disk.

The agent's only tool is ``okf_code_mode_tool``: a Code Mode tool with no host functions of its
own, over two mounts:

- ``/memory``, writable: an OKF bundle (``agent.okf_bundle_vfs_mount``) on
  :class:`~.local_disk.LocalDisk`, an ``ActivityFileSystem`` whose every operation is an
  activity on the directory named by the session's init data (``MemoryConfig``), inside this
  example's ``memories/`` folder. The tool explains OKF to the model and gives its scripts ``okf_concepts``,
  ``okf_links`` and ``okf_render``.
- ``/skills``, read-only: :class:`~.local_disk.SkillsDisk`, this example's ``skills/`` folder,
  holding the ``memory`` skill: what to remember, and how to recall, save, correct and forget.

Memory is not in the workflow: it lives on disk, so it outlasts the session, and every session
started with the same ``memory_dir`` reads and writes the same bundle. The system prompt tells
the model when to use the skill, and carries the bundle's ``index.md`` as it was on the
session's first turn, so the model can see what it remembers without a script to look.

Both mounts are declared on the class, so the console's files panel shows the paths each session
touches, and opens them from disk. ``/memory`` also has a graph of the whole bundle.
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
    from temporal_agent_harness.harness.code_mode.vfs import VFSMount

    from .local_disk import LocalDisk, LocalDiskConfig, SkillsConfig, SkillsDisk


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
`/skills/memory/SKILL.md`, then follow it. Reply in plain prose.

Below is `/memory/index.md` as it was when this session started. Use it to tell what you have \
saved and which files to read, but read a concept's file before relying on it: the memory may \
have changed since.

<memory_index>
{memory_index}
</memory_index>"""

_INDEX = PurePosixPath("index.md")


async def read_memory_index(bundle: VFSMount) -> str:
    """The bundle's top-level ``index.md``, or a note that there is none yet."""
    if await bundle.backend.stat(_INDEX) is None:
        return "(empty: nothing saved yet)"
    return (await bundle.backend.read(_INDEX)).decode()


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
        SkillsDisk,
        read_only=True,
        description="Skills, one folder each; start with its SKILL.md.",
    )
    memory = agent.okf_bundle_vfs_mount(
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
        self._memory = self.memory.bind(LocalDiskConfig(directory=data.memory_dir))
        # Read on the first turn, since init can't run activities, then kept for the session
        # so the system instruction stays the same from turn to turn.
        self._system_instruction: str | None = None
        self._code_tool = agent.okf_code_mode_tool(
            self._memory,
            name="run_code",
            mounts=[self.skills.bind(SkillsConfig())],
        )
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=3)),
            runner=self._runner,
        )

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Tell it something to remember, or ask about something you
        told it before, in this session or another one sharing its memory."""
        tools = [function_param(self._code_tool)]
        if self._system_instruction is None:
            self._system_instruction = SYSTEM_INSTRUCTION.format(
                memory_index=await read_memory_index(self._memory)
            )
        self._conversation.add_user_text(message.text)
        while True:
            stream = await self._gemini.interactions.create(
                model=MODEL,
                input=self._conversation.steps,
                system_instruction=self._system_instruction,
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
