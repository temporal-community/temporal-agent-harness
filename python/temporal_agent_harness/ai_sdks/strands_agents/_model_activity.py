from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from strands.models import Model
from strands.types.streaming import StreamEvent

from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.converter import PayloadConverter
from temporal_agent_harness.ai_sdks.strands_agents._bytes_values import (
    blank_event,
    decode_messages,
    encode_event,
    encode_messages,
)
from temporal_agent_harness.ai_sdks.strands_agents._heartbeat_decorator import auto_heartbeater
from temporal_agent_harness.ai_sdks.integration_helpers import (
    ObserverFactory,
    select_observer,
)


# Fields are typed as Any because strands TypedDicts (Message, ToolSpec) use
# NotRequired, which Python < 3.11's get_type_hints leaks through unchanged
# and the default JSON converter then fails to deserialize. Values flow
# through unchanged to ``Model.stream`` which accepts the raw dicts.
#
# Messages can hold ``bytes`` (image and document sources), which the JSON
# payload converter cannot carry: it fails on non-UTF-8 bytes and turns ASCII
# bytes into ``str``. ``__post_init__`` therefore tags the ``bytes`` fields as
# base64 (see ``_bytes_values``), and ``_stream`` restores them before the model
# sees the messages. Messages without bytes encode to the same JSON as before.
@dataclass
class _InvokeModelInput:
    model_name: str | None
    messages: Any
    invocation_state: dict[str, Any] = field(default_factory=dict)
    tool_specs: Any = None
    system_prompt: str | None = None
    tool_choice: Any = None
    system_prompt_content: Any = None
    # Sent only to the stateful model activities: the agent's model state, such
    # as the last response id.
    model_state: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.messages = encode_messages(self.messages)


@dataclass
class _InvokeModelResult:
    """A stateful model's events and its updated model state."""

    events: list[dict[str, Any]]
    model_state: dict[str, Any]


@dataclass
class _StreamingInvokeModelInput(_InvokeModelInput):
    streaming_topic: str = ""
    streaming_batch_interval_seconds: float = 0.1
    # Harness: an opaque routing token for the plugin's observer factory. When set,
    # it replaces ``streaming_topic`` publishing for this call. Typed ``Any``
    # because only the factory knows its shape; it arrives as plain JSON.
    stream_to: Any = None


@dataclass
class _ModelConfigInput:
    model_name: str | None


@dataclass
class _ModelConfig:
    """The Worker model's settings that Strands reads in Workflow code.

    Only these fields cross into History: a provider config can hold
    credentials or clients.
    """

    model_id: str | None = None
    context_window_limit: int | None = None
    stateful: bool = False


class ModelActivity:
    """Holds the registered model factories and exposes the model activities."""

    def __init__(
        self,
        factories: dict[str, Callable[[], Model]],
        *,
        default_name: str | None = None,
        observer_factory: ObserverFactory | None = None,
    ) -> None:
        """Store the factories; models are constructed lazily on first use.

        ``default_name`` is set only by the plugin's own auto-registered
        ``BedrockModel`` default. User-supplied ``models`` leave it ``None``,
        which forces every ``TemporalAgent`` to specify ``model=`` explicitly.
        """
        self._factories = factories
        self._default_name = default_name
        self._models: dict[str, Model] = {}
        # Harness: builds the live observer for calls that carry a ``stream_to`` token.
        self._observer_factory = observer_factory

    def _get_model(self, name: str | None) -> Model:
        if name is None:
            if self._default_name is None:
                raise ValueError(
                    f"TemporalAgent was constructed without an explicit `model`, "
                    f"but the plugin was configured with user-supplied `models=`. "
                    f"Pass model='...' to TemporalAgent. "
                    f"Known: {sorted(self._factories)}"
                )
            name = self._default_name
        if name not in self._models:
            if name not in self._factories:
                raise ValueError(
                    f"Unknown model name {name!r}. Known: {sorted(self._factories)}"
                )
            self._models[name] = self._factories[name]()
        return self._models[name]

    @activity.defn(name="strands_get_model_config")
    async def get_model_config(self, input: _ModelConfigInput) -> _ModelConfig:
        """Return the named model's id and context window limit."""
        model = self._get_model(input.model_name)
        config = model.get_config()
        model_id = (
            config.get("model_id")
            if isinstance(config, dict)
            else getattr(config, "model_id", None)
        )
        limit = model.context_window_limit
        return _ModelConfig(
            model_id=model_id if isinstance(model_id, str) else None,
            context_window_limit=limit if isinstance(limit, int) else None,
            stateful=bool(model.stateful),
        )

    @activity.defn
    @auto_heartbeater
    async def invoke_model(self, input: _InvokeModelInput) -> list[dict[str, Any]]:
        """Run the named model and return its stream events as a list.

        Events that carry ``bytes`` (``redactedContent``) are returned with
        that value tagged as base64; ``TemporalModel`` restores it. The result
        is typed as plain dicts because the tagged form would not validate
        against ``StreamEvent``, whose ``redactedContent`` is ``bytes``.
        """
        model = self._get_model(input.model_name)
        return [encode_event(event) async for event in _stream(model, input)]

    @activity.defn
    @auto_heartbeater
    async def invoke_model_streaming(
        self, input: _StreamingInvokeModelInput
    ) -> list[dict[str, Any]]:
        """Run the named model and publish each stream event to a WorkflowStream.

        The list this returns, which the workflow receives, carries ``bytes`` in
        their tagged form. The topic does not: subscribers are typed with
        ``StreamEvent`` (see the README), where ``redactedContent`` is ``bytes``,
        so each event is published as the payload converter writes it. See
        ``_topic_payload``.
        """
        model = self._get_model(input.model_name)
        return await _publish(_stream(model, input), input, self._observer_factory)

    # Stateful calls use their own activity types. A worker without stateful
    # support does not register them, so it cannot take such a call, drop the
    # model state and return no updated state while the agent clears its
    # messages: the call fails there and is retried until a worker with
    # stateful support takes it.
    @activity.defn(name="strands_invoke_stateful_model")
    @auto_heartbeater
    async def invoke_stateful_model(
        self, input: _InvokeModelInput
    ) -> _InvokeModelResult:
        """Run a stateful model with the agent's model state; return events and the updated state."""
        model = self._get_model(input.model_name)
        state = dict(input.model_state or {})
        events = [encode_event(event) async for event in _stream(model, input, state)]
        return _InvokeModelResult(events=events, model_state=state)

    @activity.defn(name="strands_invoke_stateful_model_streaming")
    @auto_heartbeater
    async def invoke_stateful_model_streaming(
        self, input: _StreamingInvokeModelInput
    ) -> _InvokeModelResult:
        """Run a stateful model, publish each stream event, and return events and the updated state."""
        model = self._get_model(input.model_name)
        state = dict(input.model_state or {})
        events = await _publish(
            _stream(model, input, state), input, self._observer_factory
        )
        return _InvokeModelResult(events=events, model_state=state)


async def _publish(
    stream: AsyncIterable[StreamEvent],
    input: _StreamingInvokeModelInput,
    observer_factory: ObserverFactory | None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    # Harness: a call carrying a routing token streams to the observer the factory
    # builds from it, and does not publish to ``streaming_topic``.
    if input.stream_to is not None:
        async with select_observer(
            factory=observer_factory,
            token=input.stream_to,
            batch_interval=timedelta(seconds=input.streaming_batch_interval_seconds),
        ) as observer:
            async for event in stream:
                events.append(encode_event(event))
                if observer is not None:
                    await observer.on_event(event)
        return events
    client = WorkflowStreamClient.from_within_activity(
        batch_interval=timedelta(seconds=input.streaming_batch_interval_seconds),
    )
    converter = activity.client().data_converter.payload_converter
    topic = client.topic(input.streaming_topic)
    async with client:
        async for event in stream:
            events.append(encode_event(event))
            topic.publish(_topic_payload(event, converter))
    return events


def _topic_payload(event: StreamEvent, converter: PayloadConverter) -> Payload:
    """Encode ``event`` for the streaming topic the way a ``StreamEvent`` subscriber reads it.

    A subscriber typed with ``StreamEvent`` validates ``redactedContent`` as
    ``bytes``. The raw event is what every subscriber has always received: the
    default converter writes the bytes as a list of ints and the Pydantic
    converter as a UTF-8 string, and the type hint turns either back into
    ``bytes``. The tagged form would fail that validation and end the
    subscription, so it is not used here.

    The default converter carries any ``bytes``. The Pydantic converter fails on
    bytes that are not UTF-8, and no ``StreamEvent`` subscriber could read them
    back anyway (it turns strings into ``bytes`` by UTF-8 only). Those events are
    published with ``redactedContent`` emptied, so the activity does not fail and
    the subscription goes on; the workflow still receives the real bytes in the
    activity result.
    """
    try:
        return converter.to_payload(event)
    except Exception:
        blanked = blank_event(event)
        if blanked == event:
            raise
        return converter.to_payload(blanked)


def _stream(
    model: Model, input: _InvokeModelInput, model_state: dict[str, Any] | None = None
) -> AsyncIterable[StreamEvent]:
    kwargs: dict[str, Any] = {}
    if model_state is not None:
        kwargs["model_state"] = model_state
    return model.stream(
        decode_messages(input.messages),
        input.tool_specs,
        input.system_prompt,
        tool_choice=input.tool_choice,
        system_prompt_content=input.system_prompt_content,
        invocation_state=input.invocation_state,
        **kwargs,
    )
