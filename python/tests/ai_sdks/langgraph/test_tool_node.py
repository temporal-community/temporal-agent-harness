# ABOUTME: The vendored plugin's ToolNode test for its local awrap_tool_call fix, so the
# ABOUTME: fix stays covered until the PyPI package carries it.
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import (  # pyright: ignore[reportMissingTypeStubs]
    START,
    MessagesState,
    StateGraph,
)
from langgraph.prebuilt import ToolNode  # pyright: ignore[reportMissingTypeStubs]
from langgraph.prebuilt.tool_node import (  # pyright: ignore[reportMissingTypeStubs]
    ToolCallRequest,
)
from langgraph.types import Command, Send

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.langgraph import LangGraphPlugin, graph

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


async def wrap_tool_call(
    request: ToolCallRequest,
    execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
) -> ToolMessage | Command:
    result = await execute(request)
    assert isinstance(result, ToolMessage)
    return ToolMessage(f"wrapped {result.content}", tool_call_id=result.tool_call_id)


async def send_call(state: MessagesState) -> list[Send]:  # pyright: ignore[reportUnusedParameter]
    call = {
        "name": "add",
        "args": {"a": 1, "b": 2},
        "id": "call-1",
        "type": "tool_call",
    }
    return [Send("tools", [call])]


@workflow.defn
class ToolNodeWorkflow:
    def __init__(self) -> None:
        self.app = graph("tool-node").compile()

    @workflow.run
    async def run(self) -> str:
        result = await self.app.ainvoke({"messages": []})
        return str(result["messages"][-1].content)


@pytest.mark.parametrize("execute_in", ["workflow", "activity"])
async def test_tool_node_uses_async_wrapper(client: Client, execute_in: str):
    metadata: dict[str, Any] = {"execute_in": execute_in}
    if execute_in == "activity":
        metadata["start_to_close_timeout"] = timedelta(seconds=10)
    g = StateGraph(MessagesState)
    g.add_node(
        "tools", ToolNode([add], awrap_tool_call=wrap_tool_call), metadata=metadata
    )
    g.add_conditional_edges(START, send_call, ["tools"])
    task_queue = f"tool-node-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ToolNodeWorkflow],
        plugins=[LangGraphPlugin(graphs={"tool-node": g})],
    ):
        result = await client.execute_workflow(
            ToolNodeWorkflow.run, id=f"tool-node-{uuid4()}", task_queue=task_queue
        )

    assert result == "wrapped 3"


def sync_add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


async def test_workflow_tool_node_runs_sync_tool(client: Client):
    g = StateGraph(MessagesState)
    g.add_node(
        "tools",
        ToolNode([StructuredTool.from_function(sync_add, name="add")]),
        metadata={"execute_in": "workflow"},
    )
    g.add_conditional_edges(START, send_call, ["tools"])
    task_queue = f"tool-node-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ToolNodeWorkflow],
        plugins=[LangGraphPlugin(graphs={"tool-node": g})],
    ):
        result = await client.execute_workflow(
            ToolNodeWorkflow.run, id=f"tool-node-{uuid4()}", task_queue=task_queue
        )

    assert result == "3"
