from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
    harness_tool_as_openai_tool,
)
from temporal_agent_harness.harness.agent_workflow import ToolApprovalDenied


async def test_rejected_harness_tool_is_returned_to_model() -> None:
    async def stop_writer(subagent: str) -> str:
        """Stop a writer subagent."""
        return subagent

    runner = SimpleNamespace(
        run_tool=AsyncMock(
            side_effect=ToolApprovalDenied("stop_writer", "Rejected in chat.")
        )
    )
    context = SimpleNamespace(context=runner, tool_call_id="stop-call-1")
    tool = harness_tool_as_openai_tool(stop_writer)

    result = await tool.on_invoke_tool(context, '{"subagent":"writer-1"}')

    assert result == (
        "Tool 'stop_writer' was not run: "
        "tool 'stop_writer' was not approved: Rejected in chat."
    )
    runner.run_tool.assert_awaited_once_with(
        "stop-call-1", stop_writer, "writer-1"
    )
