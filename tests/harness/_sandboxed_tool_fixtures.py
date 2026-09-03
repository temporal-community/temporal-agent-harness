# ABOUTME: Sandboxed tool definitions used by test_sandboxed_tools.py, kept in their own plain
# (non-test, not pytest-collected — leading underscore) module. remote-box re-imports a
# sandboxed tool's whole home module fresh inside the sandbox subprocess for every call, so this
# stays minimal on purpose: pulling in the actual test file (workflow classes, pytest fixtures,
# test functions) would re-execute all of that inside the subprocess too, on every single call.

import os

from pydantic import BaseModel

from temporal_agent_harness.harness import agent


class PidInput(BaseModel):
    pass


class PidResult(BaseModel):
    pid: int


@agent.activity_tool_defn(sandboxed=True)
async def get_sandbox_pid(arg: PidInput) -> PidResult:
    """A sandboxed tool: its own os.getpid() proves whether it ran in-process or out."""
    return PidResult(pid=os.getpid())


# A second sandboxed tool sharing the SAME agent, to prove one SandboxConfig covers every
# sandboxed tool an agent has — no per-tool backend wiring.
@agent.activity_tool_defn(sandboxed=True, name="get_sandbox_pid_2")
async def get_sandbox_pid_2(arg: PidInput) -> PidResult:
    """A second sandboxed tool, to prove one SandboxConfig covers every sandboxed tool."""
    return PidResult(pid=os.getpid())
