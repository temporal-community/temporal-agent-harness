"""Testing utilities for OpenAI agents."""

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from agents import (
    AgentOutputSchemaBase,
    Handoff,
    Model,
    ModelProvider,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
    Usage,
)
from agents.items import TResponseOutputItem, TResponseStreamEvent
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)

from temporalio.client import Client
from temporal_agent_harness.ai_sdks.openai_agents._mcp import (
    StatefulMCPServerProvider,
    StatelessMCPServerProvider,
)
from temporal_agent_harness.ai_sdks.openai_agents._model_parameters import ModelActivityParameters
from temporal_agent_harness.ai_sdks.openai_agents._temporal_openai_agents import OpenAIAgentsPlugin

__all__ = [
    "AgentEnvironment",
    "ResponseBuilders",
    "TestModel",
    "TestModelProvider",
    "TestStreamingModel",
]


class ResponseBuilders:
    """Builders for creating model responses for testing."""

    @staticmethod
    def model_response(output: TResponseOutputItem) -> ModelResponse:
        """Create a ModelResponse with the given output."""
        return ModelResponse(
            output=[output],
            usage=Usage(),
            response_id=None,
        )

    @staticmethod
    def response_output_message(text: str) -> ResponseOutputMessage:
        """Create a ResponseOutputMessage with text content."""
        return ResponseOutputMessage(
            id="",
            content=[
                ResponseOutputText(
                    text=text,
                    annotations=[],
                    type="output_text",
                )
            ],
            role="assistant",
            status="completed",
            type="message",
        )

    @staticmethod
    def tool_call(arguments: str, name: str) -> ModelResponse:
        """Create a ModelResponse with a function tool call."""
        return ResponseBuilders.model_response(
            ResponseFunctionToolCall(
                arguments=arguments,
                call_id="call",
                name=name,
                type="function_call",
                id="id",
                status="completed",
            )
        )

    @staticmethod
    def output_message(text: str) -> ModelResponse:
        """Create a ModelResponse with an output message."""
        return ResponseBuilders.model_response(
            ResponseBuilders.response_output_message(text)
        )

    @staticmethod
    async def stream_events(
        output_items: TResponseOutputItem | list[TResponseOutputItem],
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Synthesize the minimal real-shaped event sequence a streaming Responses API call
        emits for one turn's output item(s) (tool call(s) and/or a message), ending in
        ``response.completed`` — for use with :class:`TestStreamingModel`. Real callers
        (``agents/models/openai_responses.py``) only actually rely on the final
        ``response.completed`` event's own ``response.output`` to build the next
        ``ModelResponse`` (mid-stream ``output_item.added``/``arguments.delta`` events are
        for live display only), but a tool-call turn still emits them for realism.

        Pass a ``list`` of more than one tool-call item to simulate the model requesting
        several tool calls in the SAME turn (``parallel_tool_calls``) — the OpenAI Agents SDK
        dispatches those concurrently (``asyncio.gather``), which is what actually exercises a
        durable MCP server's own handling of overlapping ``call_tool()``s sharing one session
        (see ``test_agent_runner_calls_concurrent_tool_calls_without_racing_cleanup``); a
        single-item turn never does.

        Every event needs its OWN ``sequence_number`` (and, per event type,
        ``output_index``/etc.) set explicitly, not just the fields real callers happen to
        read directly: each event round-trips through Temporal's payload converter across
        the model activity boundary (serialize on the way out of the activity, deserialize
        on the way back into the workflow) as a *discriminated union* of every possible
        OpenAI Responses stream event type — a missing required field fails validation
        against ALL of them (not just the intended one), silently producing no event at all.
        Confirmed live: this previously round-tripped into a "Model did not produce a final
        response!" ``ModelBehaviorError``, not a validation error, since the empty result
        looked to the run loop just like a model that never finished.
        """
        items = output_items if isinstance(output_items, list) else [output_items]
        seq = 0
        for output_index, output_item in enumerate(items):
            item_id = getattr(output_item, "id", None) or f"item_{output_index}"
            if isinstance(output_item, ResponseFunctionToolCall):
                added_item = ResponseFunctionToolCall.model_construct(
                    type="function_call",
                    id=item_id,
                    call_id=output_item.call_id,
                    name=output_item.name,
                    arguments="",
                )
                yield ResponseOutputItemAddedEvent.model_construct(
                    type="response.output_item.added",
                    item=added_item,
                    output_index=output_index,
                    sequence_number=seq,
                )
                seq += 1
                yield ResponseFunctionCallArgumentsDeltaEvent.model_construct(
                    type="response.function_call_arguments.delta",
                    item_id=item_id,
                    output_index=output_index,
                    delta=output_item.arguments,
                    sequence_number=seq,
                )
                seq += 1
                yield ResponseFunctionCallArgumentsDoneEvent.model_construct(
                    type="response.function_call_arguments.done",
                    item_id=item_id,
                    output_index=output_index,
                    name=output_item.name,
                    arguments=output_item.arguments,
                    sequence_number=seq,
                )
                seq += 1

        usage = ResponseUsage.model_construct(
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            input_tokens_details=InputTokensDetails.model_construct(
                cached_tokens=0, cache_write_tokens=0
            ),
            output_tokens_details=OutputTokensDetails.model_construct(reasoning_tokens=0),
        )
        # model_construct() skips validation on the way IN, but this object still has to
        # survive a real serialize/deserialize round trip across the model activity boundary
        # (Temporal's payload converter) on the way OUT -- so every field Response.model_json_
        # schema() marks required has to actually be set, not just the ones stream_response()
        # callers happen to read directly. Missing any of these previously round-tripped into
        # a "Model did not produce a final response!" ModelBehaviorError, confirmed live.
        response_id = getattr(items[0], "id", None) if items else None
        response = Response.model_construct(
            id=f"resp_{response_id or 'empty'}",
            created_at=0.0,
            model="test-model",
            object="response",
            output=items,
            parallel_tool_calls=len(items) > 1,
            tool_choice="auto",
            tools=[],
            usage=usage,
        )
        yield ResponseCompletedEvent.model_construct(
            type="response.completed", response=response, sequence_number=seq
        )


class TestModelProvider(ModelProvider):
    """Test model provider which simply returns the given module."""

    __test__ = False

    def __init__(self, model: Model):
        """Initialize a test model provider with a model."""
        self._model = model

    def get_model(self, model_name: str | None) -> Model:
        """Get a model from the model provider."""
        return self._model


class TestModel(Model):
    """Test model for use mocking model responses."""

    __test__ = False

    def __init__(self, fn: Callable[[], ModelResponse]) -> None:
        """Initialize a test model with a callable."""
        self.fn = fn

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs: Any,
    ) -> ModelResponse:
        """Get a response from the mocked model, by calling the callable passed to the constructor."""
        return self.fn()

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs: Any,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Get a streamed response from the model. Unimplemented."""
        raise NotImplementedError()

    @staticmethod
    def returning_responses(responses: list[ModelResponse]) -> "TestModel":
        """Create a mock model which sequentially returns responses from a list."""
        i = iter(responses)
        return TestModel(lambda: next(i))


class TestStreamingModel(Model):
    """Test model for mocking ``Runner.run_streamed()`` responses.

    Unlike :class:`TestModel` (whose ``stream_response`` is unimplemented — it only supports
    ``Runner.run()``), this yields a synthetic event stream per turn via
    :func:`ResponseBuilders.stream_events`, so it can drive ``run_streamed()`` exactly like a
    real streaming API response would, without a real OpenAI API call.
    """

    __test__ = False

    def __init__(
        self, fn: Callable[[], TResponseOutputItem | list[TResponseOutputItem]]
    ) -> None:
        """Initialize a test streaming model with a callable returning the next turn's output
        item(s) -- a ``list`` of more than one simulates the model requesting several tool
        calls in the same turn (see :func:`ResponseBuilders.stream_events`)."""
        self.fn = fn

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs: Any,
    ) -> ModelResponse:
        """Unimplemented — this model only supports streaming."""
        raise NotImplementedError("TestStreamingModel only supports stream_response")

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs: Any,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Stream the next canned output item, by calling the callable passed to the
        constructor."""
        async for event in ResponseBuilders.stream_events(self.fn()):
            yield event

    @staticmethod
    def returning_responses(
        responses: list[TResponseOutputItem | list[TResponseOutputItem]],
    ) -> "TestStreamingModel":
        """Create a mock streaming model which sequentially streams turns from a list -- each
        turn a single output item, or a list of items to simulate several tool calls
        requested in the same turn."""
        i = iter(responses)
        return TestStreamingModel(lambda: next(i))


class AgentEnvironment:
    """Testing environment for OpenAI agents with Temporal integration.

    This async context manager provides a convenient way to set up testing environments
    for OpenAI agents with mocked model calls and Temporal integration.

    Example:
        >>> from temporal_agent_harness.ai_sdks.openai_agents.testing import AgentEnvironment, TestModelProvider, ResponseBuilders
        >>> from temporalio.client import Client
        >>>
        >>> # Create a mock model that returns predefined responses
        >>> mock_model = TestModel.returning_responses([
        ...     ResponseBuilders.output_message("Hello, world!"),
        ...     ResponseBuilders.output_message("How can I help you?")
        ... ])
        >>>
        >>> async with AgentEnvironment(model=mock_model) as env:
        ...     client = env.applied_on_client(client)
        ...     # Use client for testing workflows with mocked model calls
    """

    __test__ = False

    def __init__(
        self,
        model_params: ModelActivityParameters | None = None,
        model_provider: ModelProvider | None = None,
        model: Model | None = None,
        mcp_server_providers: Sequence[
            StatelessMCPServerProvider | StatefulMCPServerProvider
        ] = (),
        register_activities: bool = True,
        add_temporal_spans: bool = True,
        use_otel_instrumentation: bool = False,
        nexus_transport: bool = False,
    ) -> None:
        """Initialize the AgentEnvironment.

        Args:
            model_params: Configuration parameters for Temporal activity execution
                of model calls. If None, default parameters will be used.
            model_provider: Optional model provider for custom model implementations.
                Only one of model_provider or model should be provided.
                If both are provided, model_provider will be used.
            model: Optional model for custom model implementations.
                Use TestModel for mocking model responses.
                Equivalent to model_provider=TestModelProvider(model).
                Only one of model_provider or model should be provided.
                If both are provided, model_provider will be used.
            mcp_server_providers: Sequence of MCP servers to automatically register with the worker.
            register_activities: Whether to register activities during worker execution.
            add_temporal_spans: Whether to add temporal spans to traces
            use_otel_instrumentation: If set to true, enable open telemetry instrumentation.
                Warning: use_otel_instrumentation is experimental and behavior may change in future versions.
                Use with caution in production environments.
            nexus_transport: See ``OpenAIAgentsPlugin.__init__``'s matching parameter.
        """
        self._model_params = model_params
        self._model_provider = None
        if model_provider is not None:
            self._model_provider = model_provider
        elif model is not None:
            self._model_provider = TestModelProvider(model)
        self._mcp_server_providers = mcp_server_providers
        self._register_activities = register_activities
        self._plugin: OpenAIAgentsPlugin | None = None
        self._add_temporal_spans = add_temporal_spans
        self._use_otel_instrumentation = use_otel_instrumentation
        self._nexus_transport = nexus_transport

    async def __aenter__(self) -> "AgentEnvironment":
        """Enter the async context manager."""
        # Create the plugin with the provided configuration
        self._plugin = OpenAIAgentsPlugin(
            model_params=self._model_params,
            model_provider=self._model_provider,
            mcp_server_providers=self._mcp_server_providers,
            register_activities=self._register_activities,
            add_temporal_spans=self._add_temporal_spans,
            use_otel_instrumentation=self._use_otel_instrumentation,
            nexus_transport=self._nexus_transport,
        )

        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit the async context manager."""
        # No cleanup needed currently
        pass

    def applied_on_client(self, client: Client) -> Client:
        """Apply the agent environment's plugin to a client and return a new client instance.

        Args:
            client: The base Temporal client to apply the plugin to.

        Returns:
            A new Client instance with the OpenAI agents plugin applied.
        """
        if self._plugin is None:
            raise RuntimeError(
                "AgentEnvironment must be entered before applying to client"
            )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [self._plugin]
        return Client(**new_config)

    @property
    def openai_agents(self) -> OpenAIAgentsPlugin:
        """Get the underlying OpenAI agents plugin."""
        if self._plugin is None:
            raise RuntimeError(
                "AgentEnvironment must be entered before accessing plugin"
            )
        return self._plugin
