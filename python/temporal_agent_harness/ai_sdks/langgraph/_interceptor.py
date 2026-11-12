"""Workflow interceptor that scopes LangGraph graphs/entrypoints to the workflow run."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import sys
from types import FrameType
from typing import Any, Callable

from langgraph.graph import StateGraph
from langgraph.pregel import Pregel

from temporalio import workflow
from temporalio.contrib.workflow_streams._stream import _PUBLISH_SIGNAL
from temporal_agent_harness.ai_sdks.langgraph._activity import clear_store_warning
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

_workflow_graphs: dict[str, dict[str, StateGraph[Any, Any, Any, Any]]] = {}
_workflow_entrypoints: dict[str, dict[str, Pregel[Any, Any, Any, Any]]] = {}


class LangGraphInterceptor(Interceptor):
    """Interceptor that registers a workflow's graphs and entrypoints for the run."""

    def __init__(
        self,
        graphs: dict[str, StateGraph[Any, Any, Any, Any]],
        entrypoints: dict[str, Pregel[Any, Any, Any, Any]],
        streaming_topic: str | None = None,
    ) -> None:
        """Initialize with the graphs and entrypoints to scope to each workflow run."""
        self._graphs = graphs
        self._entrypoints = entrypoints
        self._streaming_topic = streaming_topic

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        """Return the inbound interceptor class used to scope graphs per run."""
        graphs = self._graphs
        entrypoints = self._entrypoints
        streaming_topic = self._streaming_topic

        class Inbound(WorkflowInboundInterceptor):
            def init(self, outbound: WorkflowOutboundInterceptor) -> None:
                run_id = outbound.info().run_id
                _workflow_graphs[run_id] = graphs
                _workflow_entrypoints[run_id] = entrypoints
                super().init(outbound)

            async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
                _run_langchain_sync_calls_inline(asyncio.get_running_loop())
                if (
                    streaming_topic is not None
                    and workflow.get_signal_handler(_PUBLISH_SIGNAL) is None
                ):
                    raise RuntimeError(
                        f"LangGraphPlugin was configured with "
                        f"streaming_topic={streaming_topic!r}, but workflow "
                        f"{workflow.info().workflow_type!r} did not register a "
                        f"WorkflowStream. Construct WorkflowStream() in the "
                        f"workflow's @workflow.init (i.e. __init__) method so "
                        f"streaming activities can publish to it."
                    )
                try:
                    return await self.next.execute_workflow(input)
                finally:
                    run_id = workflow.info().run_id
                    _workflow_graphs.pop(run_id, None)
                    _workflow_entrypoints.pop(run_id, None)
                    clear_store_warning(run_id)

        return Inbound


def _run_langchain_sync_calls_inline(loop: asyncio.AbstractEventLoop) -> None:
    """Make the workflow event loop run LangChain's thread calls inline.

    LangGraph runs a sync routing function (``add_conditional_edges``) with
    LangChain's ``run_in_executor`` helper, which calls
    ``loop.run_in_executor``. The workflow event loop has no threads and raised
    ``NotImplementedError``, so the run never finished. Only calls from that
    helper run inline; any other ``run_in_executor`` call fails as before.
    """
    _run_inline_when_called_by(loop, _called_by_langchain)


# The callers whose run_in_executor calls the workflow loop runs inline. A
# plugin that adds its own caller uses this same list on the same loop, so with
# several plugins one wrapper checks the original caller against all of them.
_INLINE_CALLERS = "_temporal_inline_executor_callers"


def _run_inline_when_called_by(
    loop: asyncio.AbstractEventLoop, is_caller: Callable[[FrameType], bool]
) -> None:
    """Make the workflow loop run ``run_in_executor`` inline for this caller."""
    callers: list[Callable[[FrameType], bool]] | None = getattr(
        loop, _INLINE_CALLERS, None
    )
    if callers is None:
        callers = []
        run_in_executor = loop.run_in_executor

        def inline_run_in_executor(
            executor: Any, func: Callable[..., Any], *args: Any
        ) -> asyncio.Future[Any]:
            caller = sys._getframe(1)
            if not any(called_by(caller) for called_by in callers):
                return run_in_executor(executor, func, *args)
            future = loop.create_future()
            future.set_result(func(*args))
            return future

        setattr(loop, "run_in_executor", inline_run_in_executor)
        setattr(loop, _INLINE_CALLERS, callers)
    if is_caller not in callers:
        callers.append(is_caller)


def _called_by_langchain(frame: FrameType) -> bool:
    """Whether the caller is ``langchain_core.runnables.config.run_in_executor``."""
    return (
        frame.f_code.co_name == "run_in_executor"
        and frame.f_globals.get("__name__") == "langchain_core.runnables.config"
    )
