import asyncio
import json
from collections.abc import AsyncIterable, Callable
from datetime import timedelta
from typing import Any

from strands.models import Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.exceptions import ContextWindowOverflowException
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from temporalio import workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ActivityCancellationType, VersioningIntent

from temporal_agent_harness.ai_sdks.strands_agents._bytes_values import decode_event
from temporal_agent_harness.ai_sdks.strands_agents._model_activity import (
    ModelActivity,
    _InvokeModelInput,
    _InvokeModelResult,
    _ModelConfig,
    _ModelConfigInput,
    _StreamingInvokeModelInput,
)

# Patch id for rebuilding ContextWindowOverflowException from a failed model Activity.
# Never change it: it is recorded in histories as a marker.
_CONTEXT_WINDOW_OVERFLOW_PATCH = "strands-agents.context-window-overflow-recovery"


def _filter_serializable(state: dict[str, Any]) -> dict[str, Any]:
    """Keep invocation_state entries that JSON-serialize; drop the rest with a debug log."""
    clean: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in state.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            dropped.append(key)
            continue
        clean[key] = value
    if dropped:
        workflow.logger.debug(
            f"Dropping non-serializable invocation_state keys: {dropped}"
        )
    return clean


class TemporalModel(Model):
    """A Strands ``Model`` that runs ``stream()`` as a Temporal activity.

    ``model_name`` selects which factory the plugin will invoke worker-side; it
    must match a key in ``StrandsPlugin(models={...})``. Construction of this
    ``TemporalModel`` itself does no I/O, so it is safe to instantiate at
    module level.

    When ``streaming_topic`` is set, each ``StreamEvent`` is also published to
    the named topic on the workflow's
    :class:`temporalio.contrib.workflow_streams.WorkflowStream` for external
    consumers.
    """

    def __init__(
        self,
        model_name: str | None = None,
        *,
        task_queue: str | None = None,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        retry_policy: RetryPolicy | None = None,
        cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
        versioning_intent: VersioningIntent | None = None,
        summary: str | None = None,
        priority: Priority = Priority.default,
        streaming_topic: str | None = None,
        streaming_batch_interval: timedelta = timedelta(milliseconds=100),
        # Harness: per-call routing token for the plugin's observer factory.
        stream_route: Callable[[str | None], Any] | None = None,
    ) -> None:
        """Configure the model name, activity options, and streaming settings."""
        self._model_name = model_name
        self._stream_route = stream_route
        self._streaming_topic = streaming_topic
        self._streaming_batch_interval = streaming_batch_interval
        self._options: dict[str, Any] = {
            "task_queue": task_queue,
            "schedule_to_close_timeout": schedule_to_close_timeout,
            "schedule_to_start_timeout": schedule_to_start_timeout,
            "start_to_close_timeout": start_to_close_timeout,
            "heartbeat_timeout": heartbeat_timeout,
            "retry_policy": retry_policy,
            "cancellation_type": cancellation_type,
            "versioning_intent": versioning_intent,
            "summary": summary,
            "priority": priority,
        }
        self._config: dict[str, Any] = {}
        self._config_loaded = False
        self._config_load: asyncio.Task[None] | None = None
        self._stateful = False

    def update_config(self, **_model_config: Any) -> None:
        """No-op; the real model is configured worker-side via the plugin's factories."""
        return None

    def get_config(self) -> dict[str, Any]:
        """Return the worker-side model's ``model_id`` and ``context_window_limit``.

        They are empty until ``TemporalAgent`` loads them before its first
        invocation.
        """
        return dict(self._config)

    @property
    def config(self) -> dict[str, Any]:
        """The loaded config; Strands' tracing reads ``model_id`` from it."""
        return self._config

    @property
    def stateful(self) -> bool:
        """Whether the worker-side model keeps the conversation server-side."""
        return self._stateful

    async def _load_config(self) -> None:
        """Read the worker-side model's config once, as an activity.

        Callers that overlap share one read: a later invocation after the
        first was cancelled, or reentrant invocations. A caller's cancellation
        does not cancel the shared read; a failed read is retried by the next
        invocation.
        """
        if self._config_loaded:
            return
        if self._config_load is None:
            self._config_load = asyncio.create_task(self._read_config())
        await asyncio.shield(self._config_load)

    async def _read_config(self) -> None:
        try:
            result: _ModelConfig = await workflow.execute_activity_method(
                ModelActivity.get_model_config,
                _ModelConfigInput(model_name=self._model_name),
                **self._options,
            )
        except BaseException:
            self._config_load = None
            raise
        if result.model_id is not None:
            self._config["model_id"] = result.model_id
        if result.context_window_limit is not None:
            self._config["context_window_limit"] = result.context_window_limit
        self._stateful = result.stateful
        self._config_loaded = True

    def structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        """Not supported; use ``TemporalAgent(structured_output_model=...)`` instead."""
        raise NotImplementedError(
            "TemporalModel.structured_output is not supported. Use "
            "TemporalAgent(structured_output_model=...) which routes structured "
            "output through stream() via the structured_output_tool."
        )

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        """Run the model via the registered Temporal activity and yield events."""
        clean_state = _filter_serializable(invocation_state) if invocation_state else {}
        # A stateful model gets a copy of the agent's model state and returns the
        # updated state, through activity types that only workers with stateful
        # support register.
        model_state: dict[str, Any] | None = kwargs.get("model_state")
        sent_state = dict(model_state or {}) if self._stateful else None
        # Harness: resolve the call's routing token in workflow code.
        stream_to = (
            self._stream_route(self._config.get("model_id") or self._model_name)
            if self._stream_route is not None
            else None
        )
        try:
            if self._streaming_topic is not None or stream_to is not None:
                streaming_input = _StreamingInvokeModelInput(
                    model_name=self._model_name,
                    messages=messages,
                    invocation_state=clean_state,
                    tool_specs=tool_specs,
                    system_prompt=system_prompt,
                    tool_choice=tool_choice,
                    system_prompt_content=system_prompt_content,
                    streaming_topic=self._streaming_topic or "",
                    streaming_batch_interval_seconds=self._streaming_batch_interval.total_seconds(),
                    model_state=sent_state,
                    stream_to=stream_to,
                )
                if sent_state is None:
                    events = await workflow.execute_activity_method(
                        ModelActivity.invoke_model_streaming,
                        streaming_input,
                        **self._options,
                    )
                else:
                    result = await workflow.execute_activity_method(
                        ModelActivity.invoke_stateful_model_streaming,
                        streaming_input,
                        **self._options,
                    )
                    events = _apply_state(result, model_state)
            else:
                invoke_input = _InvokeModelInput(
                    model_name=self._model_name,
                    messages=messages,
                    invocation_state=clean_state,
                    tool_specs=tool_specs,
                    system_prompt=system_prompt,
                    tool_choice=tool_choice,
                    system_prompt_content=system_prompt_content,
                    model_state=sent_state,
                )
                if sent_state is None:
                    events = await workflow.execute_activity_method(
                        ModelActivity.invoke_model,
                        invoke_input,
                        **self._options,
                    )
                else:
                    result = await workflow.execute_activity_method(
                        ModelActivity.invoke_stateful_model,
                        invoke_input,
                        **self._options,
                    )
                    events = _apply_state(result, model_state)
        except ActivityError as error:
            # Model providers raise ContextWindowOverflowException when the prompt is
            # too long, and Strands recovers by reducing the conversation and retrying.
            # The activity fails with a typed ApplicationError, so rebuild the Strands
            # exception here, where Strands' event loop can catch it. Histories recorded
            # before this change let the ActivityError propagate, and a Workflow may have
            # handled it by scheduling other work, so they keep that behavior.
            cause = error.__cause__
            if (
                isinstance(cause, ApplicationError)
                and cause.type == ContextWindowOverflowException.__name__
                and workflow.patched(_CONTEXT_WINDOW_OVERFLOW_PATCH)
            ):
                raise ContextWindowOverflowException(cause.message) from error
            raise
        for event in events:
            yield decode_event(event)


def _apply_state(
    result: _InvokeModelResult, model_state: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Copy a stateful model's updated state into the agent's and return its events."""
    if model_state is not None:
        model_state.clear()
        model_state.update(result.model_state)
    return result.events
