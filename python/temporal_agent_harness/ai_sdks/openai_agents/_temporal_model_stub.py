from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agents import (
    Agent,
    AgentOutputSchema,
    AgentOutputSchemaBase,
    CodeInterpreterTool,
    FileSearchTool,
    FunctionTool,
    Handoff,
    HostedMCPTool,
    ImageGenerationTool,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
    WebSearchTool,
)
from agents.items import TResponseStreamEvent
from agents.tool import (
    ApplyPatchTool,
    CustomTool,
    LocalShellTool,
    ShellTool,
    ToolSearchTool,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam

from temporalio import workflow
from temporal_agent_harness.ai_sdks.openai_agents._invoke_model_activity import (
    ActivityModelInput,
    AgentOutputSchemaInput,
    ApplyPatchToolInput,
    CustomToolInput,
    FunctionToolInput,
    HandoffInput,
    HostedMCPToolInput,
    ModelActivity,
    ModelTracingInput,
    ShellToolInput,
    StreamingActivityModelInput,
    ToolInput,
)
from temporal_agent_harness.ai_sdks.openai_agents._model_parameters import ModelActivityParameters


class _TemporalModelStub(Model):  # type:ignore[reportUnusedClass]
    """A stub that allows invoking models as Temporal activities."""

    def __init__(
        self,
        model_name: str | None,
        *,
        model_params: ModelActivityParameters,
        agent: Agent[Any] | None,
        run_context: Any = None,
    ) -> None:
        self.model_name = model_name
        self.model_params = model_params
        self.agent = agent
        # The object the caller passed as ``Runner.run_streamed(..., context=...)``,
        # threaded in workflow-side by the runner. Opaque here; handed to
        # ``stream_to_provider`` so an embedding runtime can turn its own handle
        # (e.g. a per-workflow runner) into a routing token. Unused by non-streaming
        # calls.
        self._run_context = run_context

    def _build_activity_input(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> tuple[ActivityModelInput, str | None]:
        def make_tool_info(tool: Tool) -> ToolInput:
            if isinstance(
                tool,
                (
                    FileSearchTool,
                    WebSearchTool,
                    ImageGenerationTool,
                    CodeInterpreterTool,
                    LocalShellTool,
                    ToolSearchTool,
                ),
            ):
                return tool
            elif isinstance(tool, ShellTool):
                return ShellToolInput(
                    name=tool.name,
                    environment=tool.environment,
                )
            elif isinstance(tool, ApplyPatchTool):
                return ApplyPatchToolInput(name=tool.name)
            elif isinstance(tool, HostedMCPTool):
                return HostedMCPToolInput(tool_config=tool.tool_config)
            elif isinstance(tool, CustomTool):
                return CustomToolInput(tool_config=tool.tool_config)
            elif isinstance(tool, FunctionTool):
                return FunctionToolInput(
                    name=tool.name,
                    description=tool.description,
                    params_json_schema=tool.params_json_schema,
                    strict_json_schema=tool.strict_json_schema,
                )
            else:
                raise ValueError(f"Unsupported tool type: {tool.name}")

        tool_infos = [make_tool_info(x) for x in tools]
        handoff_infos = [
            HandoffInput(
                tool_name=x.tool_name,
                tool_description=x.tool_description,
                input_json_schema=x.input_json_schema,
                agent_name=x.agent_name,
                strict_json_schema=x.strict_json_schema,
            )
            for x in handoffs
        ]
        if output_schema is not None and not isinstance(
            output_schema, AgentOutputSchema
        ):
            raise TypeError(
                f"Only AgentOutputSchema is supported by Temporal Model, got {type(output_schema).__name__}"
            )
        agent_output_schema = output_schema
        output_schema_input = (
            None
            if agent_output_schema is None
            else AgentOutputSchemaInput(
                output_type_name=agent_output_schema.name(),
                is_wrapped=agent_output_schema._is_wrapped,
                output_schema=agent_output_schema.json_schema()
                if not agent_output_schema.is_plain_text()
                else None,
                strict_json_schema=agent_output_schema.is_strict_json_schema(),
            )
        )

        activity_input = ActivityModelInput(
            model_name=self.model_name,
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tool_infos,
            output_schema=output_schema_input,
            handoffs=handoff_infos,
            tracing=ModelTracingInput(tracing.value),
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

        if self.model_params.summary_override:
            summary = (
                self.model_params.summary_override
                if isinstance(self.model_params.summary_override, str)
                else (
                    self.model_params.summary_override.provide(
                        self.agent, system_instructions, input
                    )
                )
            )
        elif self.agent:
            summary = self.agent.name
        else:
            summary = None

        return activity_input, summary

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        activity_input, summary = self._build_activity_input(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

        if self.model_params.use_local_activity:
            return await workflow.execute_local_activity_method(
                ModelActivity.invoke_model_activity,
                activity_input,
                summary=summary,
                schedule_to_close_timeout=self.model_params.schedule_to_close_timeout,
                schedule_to_start_timeout=self.model_params.schedule_to_start_timeout,
                start_to_close_timeout=self.model_params.start_to_close_timeout,
                retry_policy=self.model_params.retry_policy,
                cancellation_type=self.model_params.cancellation_type,
            )
        return await workflow.execute_activity_method(
            ModelActivity.invoke_model_activity,
            activity_input,
            summary=summary,
            task_queue=self.model_params.task_queue,
            schedule_to_close_timeout=self.model_params.schedule_to_close_timeout,
            schedule_to_start_timeout=self.model_params.schedule_to_start_timeout,
            start_to_close_timeout=self.model_params.start_to_close_timeout,
            heartbeat_timeout=self.model_params.heartbeat_timeout,
            retry_policy=self.model_params.retry_policy,
            cancellation_type=self.model_params.cancellation_type,
            versioning_intent=self.model_params.versioning_intent,
            priority=self.model_params.priority,
        )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        # Streaming relies on activity heartbeats to detect a stuck LLM
        # call and on WorkflowStreamClient.from_within_activity() to signal
        # partial results back to the workflow. Local activities support
        # neither: their result commits with the workflow task, so there
        # is no independent task to heartbeat against or to send signals
        # from.
        if self.model_params.use_local_activity:
            raise ValueError(
                "Streaming is incompatible with use_local_activity "
                "(local activities do not support heartbeats or the "
                "workflow stream signal channel)."
            )

        # Resolve the opaque per-call routing token: prefer the configured
        # provider (called here, in workflow context), falling back to the
        # static streaming_topic. The activity hands whatever this is to its
        # observer factory; it never inspects the token itself.
        stream_to: Any = None
        if self.model_params.stream_to_provider is not None:
            # Pass the requested model id and the caller's run context, so the
            # provider can turn its threaded handle into a routing token (and let
            # the observer name the model when it brackets the call at dispatch).
            stream_to = self.model_params.stream_to_provider(
                self.model_name, self._run_context
            )
        if stream_to is None:
            stream_to = self.model_params.streaming_topic
        if stream_to is None:
            raise ValueError(
                "Runner.run_streamed requires ModelActivityParameters."
                "streaming_topic or a stream_to_provider that returns a "
                "routing token."
            )

        base_input, summary = self._build_activity_input(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )
        streaming_input: StreamingActivityModelInput = {
            **base_input,
            "stream_to": stream_to,
            "streaming_batch_interval": self.model_params.streaming_batch_interval,
        }

        events = await workflow.execute_activity_method(
            ModelActivity.invoke_model_activity_streaming,
            streaming_input,
            summary=summary,
            task_queue=self.model_params.task_queue,
            schedule_to_close_timeout=self.model_params.schedule_to_close_timeout,
            schedule_to_start_timeout=self.model_params.schedule_to_start_timeout,
            start_to_close_timeout=self.model_params.start_to_close_timeout,
            heartbeat_timeout=self.model_params.heartbeat_timeout,
            retry_policy=self.model_params.retry_policy,
            cancellation_type=self.model_params.cancellation_type,
            versioning_intent=self.model_params.versioning_intent,
            priority=self.model_params.priority,
        )
        for event in events:
            yield event
