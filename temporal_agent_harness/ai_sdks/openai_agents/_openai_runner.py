import dataclasses
from collections.abc import AsyncIterator, Awaitable
from typing import Any, Callable, cast

from agents import (
    Agent,
    AgentsException,
    Handoff,
    RunConfig,
    RunContextWrapper,
    RunResult,
    RunResultStreaming,
    RunState,
    SQLiteSession,
    TContext,
    TResponseInputItem,
)
from agents.mcp import MCPServer
from agents.run import DEFAULT_AGENT_RUNNER, AgentRunner, RunOptions
from agents.sandbox import SandboxAgent
from typing_extensions import Unpack

from temporalio import workflow
from temporal_agent_harness.ai_sdks.openai_agents._model_parameters import ModelActivityParameters
from temporal_agent_harness.ai_sdks.openai_agents._temporal_model_stub import _TemporalModelStub
from temporal_agent_harness.ai_sdks.openai_agents.sandbox._temporal_sandbox_client import (
    TemporalSandboxClient,
)
from temporal_agent_harness.ai_sdks.openai_agents.workflow import AgentsWorkflowError

# One fresh transport per Runner.run()/run_streamed() call (one per "turn"), NOT shared
# across the whole workflow execution — cleaned up (see run()/run_streamed() below) once
# that call finishes. This mirrors exactly the lifecycle of the original, manual
# `async with nexus_transport_mcp_server(...) as mcp_server:` pattern, just performed
# automatically by the runner instead of by hand at the call site.
#
# Historical note, no longer applicable to the CURRENT _NexusTransportMCPServer (see
# _nexus_mcp.py's module docstring): an early version shared one instance for the whole
# workflow, backed by a real (if in-process/fake) MCP ClientSession with its own background
# router task. Never disconnecting that broke workflow eviction (the router task never got
# cancelled, so Temporal's SDK couldn't cleanly tear down the workflow's event loop, hanging
# "eviction" for minutes at a time). _NexusTransportMCPServer no longer has a session or
# router task at all — connect()/cleanup() are no-ops — so that specific failure mode is
# gone; fresh-per-call construction is kept anyway since it costs nothing (no session to
# reconnect) and keeps this runner's contract simple: whatever it injects, it also cleans up.
def _create_nexus_transport_mcp_server() -> MCPServer:
    from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
        nexus_transport_mcp_server,
    )

    # nexus_transport_mcp_server()'s return type is AbstractAsyncContextManager[MCPServer]
    # for its own `async with nexus_transport_mcp_server() as mcp_server:` usage pattern --
    # but the concrete object is an MCPServer directly too (its __aenter__ just returns
    # self), and this call site deliberately doesn't use `async with`: this runner manages
    # the lifecycle itself (see run()/run_streamed()'s own cleanup calls below).
    return cast(MCPServer, nexus_transport_mcp_server(name="nexus-transport"))


async def _cleanup_nexus_transport_mcp_server(server: MCPServer) -> None:
    try:
        await server.cleanup()
    except Exception:
        workflow.logger.warning(
            "[nexus-transport] cleanup failed for the auto-injected MCP server", exc_info=True
        )


# Recursively replace models in all agents
def _convert_agent(
    model_params: ModelActivityParameters,
    agent: Agent[Any],
    seen: dict[int, Agent] | None,
    run_context: Any = None,
    nexus_transport_mcp_server: MCPServer | None = None,
) -> Agent[Any]:
    if seen is None:
        seen = dict()

    # Short circuit if this model was already seen to prevent looping from circular handoffs
    if id(agent) in seen:
        return seen[id(agent)]

    # This agent has already been processed in some other run
    if isinstance(agent.model, _TemporalModelStub):
        return agent

    # Save the new version of the agent so that we can replace loops
    new_agent = dataclasses.replace(agent)
    seen[id(agent)] = new_agent

    name = _model_name(agent)

    new_handoffs: list[Agent | Handoff] = []
    for handoff in agent.handoffs:
        if isinstance(handoff, Agent):
            new_handoffs.append(
                _convert_agent(
                    model_params, handoff, seen, run_context, nexus_transport_mcp_server
                )
            )
        elif isinstance(handoff, Handoff):
            original_invoke = handoff.on_invoke_handoff

            # Use default parameter to capture original_invoke by value, not reference
            async def on_invoke(
                context: RunContextWrapper[Any],
                args: str,
                invoke_func: Callable[
                    [RunContextWrapper[Any], str], Awaitable[Any]
                ] = original_invoke,
                run_context: Any = run_context,
            ) -> Agent:
                handoff_agent = await invoke_func(context, args)
                return _convert_agent(
                    model_params,
                    handoff_agent,
                    seen,
                    run_context,
                    nexus_transport_mcp_server,
                )

            new_handoffs.append(
                dataclasses.replace(handoff, on_invoke_handoff=on_invoke)
            )
        else:
            raise TypeError(f"Unknown handoff type: {type(handoff)}")

    new_agent.model = _TemporalModelStub(
        model_name=name,
        model_params=model_params,
        agent=agent,
        run_context=run_context,
    )
    new_agent.handoffs = new_handoffs

    if nexus_transport_mcp_server is not None:
        from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import (
            _NexusTransportMCPServer,
        )

        if not any(
            isinstance(s, _NexusTransportMCPServer) for s in new_agent.mcp_servers
        ):
            new_agent.mcp_servers = [*new_agent.mcp_servers, nexus_transport_mcp_server]

    return new_agent


def _has_sandbox_agent(agent: Agent[Any], seen: set[int] | None = None) -> bool:
    """Check if any agent in the graph (following direct Agent handoffs) is a SandboxAgent."""
    if seen is None:
        seen = set()
    if id(agent) in seen:
        return False
    seen.add(id(agent))
    if isinstance(agent, SandboxAgent):
        return True
    for handoff in agent.handoffs:
        if isinstance(handoff, Agent) and _has_sandbox_agent(handoff, seen):
            return True
    return False


class TemporalOpenAIRunner(AgentRunner):
    """Temporal Runner for OpenAI agents.

    Forwards model calls to a Temporal activity.

    """

    def __init__(
        self,
        model_params: ModelActivityParameters,
        nexus_transport: bool = False,
    ) -> None:
        """Initialize the Temporal OpenAI Runner.

        Args:
            model_params: Configuration for the model-call activity.
            nexus_transport: If true, every ``Agent`` run in a workflow automatically gets a
                Nexus-transport MCP server (``workflow.nexus_transport_mcp_server``) appended
                to its ``mcp_servers`` — no explicit ``mcp_servers=[...]`` wiring needed at
                any call site. Skipped for an agent that already has one (e.g. a
                hand-constructed instance with custom options). What that transport can
                actually reach is entirely a function of what's registered live against the
                calling workflow's own registry (see ``NexusMcpServerRegistry``) — a plain
                URL-based MCP server a caller configures directly on an ``Agent`` is
                untouched either way, since injection is additive. A fresh transport is
                constructed for each ``run()``/``run_streamed()`` call (one per conversation
                turn) and cleaned up once that call completes — both are cheap, in-memory-only
                no-ops (``_NexusTransportMCPServer`` calls straight through to
                ``WorkflowTransport`` on every ``list_tools``/``call_tool``, no session or
                connection of any kind to open or hold — see ``_nexus_mcp.py``'s module
                docstring), so this does not add a real per-turn cost. See
                ``OpenAIAgentsPlugin.__init__``'s matching parameter.
        """
        self._runner = DEFAULT_AGENT_RUNNER or AgentRunner()
        self.model_params = model_params
        self._nexus_transport = nexus_transport

    def _prepare_workflow_run(
        self,
        starting_agent: Agent[TContext],
        kwargs: RunOptions[TContext],
    ) -> tuple[Agent[Any], MCPServer | None]:
        """Workflow-only validation and ``kwargs`` rewrite shared by ``run()`` and
        ``run_streamed()``.

        Returns the converted agent plus the auto-injected Nexus-transport MCP server (if
        ``nexus_transport`` is enabled), so the caller can clean it up once its run
        completes — see ``_cleanup_nexus_transport_mcp_server``.
        """
        for t in starting_agent.tools:
            if callable(t):
                raise ValueError(
                    "Provided tool is not a tool type. If using an activity, make sure to wrap it with openai_agents.workflow.activity_as_tool."
                )

        if starting_agent.mcp_servers:
            from temporal_agent_harness.ai_sdks.openai_agents._mcp import (
                _DurableMCPServerMarker,
            )

            for s in starting_agent.mcp_servers:
                if not isinstance(s, _DurableMCPServerMarker):
                    raise ValueError(
                        f"Unknown mcp_server type {type(s)} may not work durably."
                    )

        nexus_transport_mcp_server = None
        if self._nexus_transport:
            nexus_transport_mcp_server = _create_nexus_transport_mcp_server()

        if isinstance(kwargs.get("session"), SQLiteSession):
            raise ValueError("Temporal workflows don't support SQLite sessions.")

        # The object the caller threaded via ``Runner.run_streamed(..., context=...)``.
        # Captured here (synchronously, before the framework starts its run task) and
        # handed to each model stub, which forwards it to ``stream_to_provider`` to
        # resolve the live stream routing token.
        run_context = kwargs.get("context")

        run_config = kwargs.get("run_config")
        if run_config is None:
            run_config = RunConfig()

        if run_config.model and not isinstance(run_config.model, _TemporalModelStub):
            if not isinstance(run_config.model, str):
                raise ValueError(
                    "Temporal workflows require a model name to be a string in the run config."
                )
            run_config = dataclasses.replace(
                run_config,
                model=_TemporalModelStub(
                    run_config.model,
                    model_params=self.model_params,
                    agent=None,
                    run_context=run_context,
                ),
            )

        # run_config.sandbox is global for the entire run — configure it if any agent needs it.
        if _has_sandbox_agent(starting_agent) or run_config.sandbox:
            if run_config.sandbox is None:
                raise ValueError(
                    "A SandboxAgent was provided but run_config.sandbox is not configured. "
                    "You must set run_config.sandbox to a SandboxRunConfig. "
                    "For example:\n"
                    "  from temporal_agent_harness.ai_sdks.openai_agents.workflow import temporal_sandbox_client\n"
                    "  run_config = RunConfig(sandbox=SandboxRunConfig(client=temporal_sandbox_client('my-backend')))"
                )
            elif run_config.sandbox.client is None:
                raise ValueError(
                    "run_config.sandbox.client must be set to a temporal sandbox client. "
                    "Use temporal_agent_harness.ai_sdks.openai_agents.workflow.temporal_sandbox_client(name) "
                    "to create one, where name matches a SandboxClientProvider registered on the plugin."
                )
            elif not isinstance(run_config.sandbox.client, TemporalSandboxClient):
                raise ValueError(
                    "run_config.sandbox.client must be created via "
                    "temporal_agent_harness.ai_sdks.openai_agents.workflow.temporal_sandbox_client(name). "
                    "Do not pass a raw sandbox client directly."
                )

        kwargs["run_config"] = run_config
        converted_agent = _convert_agent(
            self.model_params, starting_agent, None, run_context, nexus_transport_mcp_server
        )
        return converted_agent, nexus_transport_mcp_server

    async def run(
        self,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        **kwargs: Unpack[RunOptions[TContext]],
    ) -> RunResult:
        """Run the agent in a Temporal workflow."""
        if not workflow.in_workflow():
            return await self._runner.run(
                starting_agent,
                input,
                **kwargs,
            )

        converted_agent, injected_server = self._prepare_workflow_run(starting_agent, kwargs)

        try:
            return await self._runner.run(
                starting_agent=converted_agent,
                input=input,
                **kwargs,
            )
        except AgentsException as e:
            # In order for workflow failures to properly fail the workflow, we need to rewrap them in
            # a Temporal error
            if e.__cause__ and workflow.is_failure_exception(e.__cause__):
                reraise = AgentsWorkflowError(
                    f"Workflow failure exception in Agents Framework: {e}"
                )
                reraise.__traceback__ = e.__traceback__
                raise reraise from e.__cause__
            else:
                raise e
        finally:
            if injected_server is not None:
                await _cleanup_nexus_transport_mcp_server(injected_server)

    def run_sync(
        self,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        **kwargs: Any,
    ) -> RunResult:
        """Run the agent synchronously (not supported in Temporal workflows)."""
        if not workflow.in_workflow():
            return self._runner.run_sync(
                starting_agent,
                input,
                **kwargs,
            )
        raise RuntimeError("Temporal workflows do not support synchronous model calls.")

    def run_streamed(
        self,
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem] | RunState[TContext],
        **kwargs: Unpack[RunOptions[TContext]],
    ) -> RunResultStreaming:
        """Run the agent with streaming responses.

        .. warning::
            Streaming inside Temporal workflows is experimental and may
            change in future versions.

        Inside a workflow, model calls execute as the streaming model
        activity. The workflow consumes events via
        ``RunResultStreaming.stream_events()`` after each activity
        completes; external clients can subscribe to the configured
        stream topic to receive events as they arrive.
        """
        if not workflow.in_workflow():
            return self._runner.run_streamed(
                starting_agent,
                input,
                **kwargs,
            )

        # Fail-fast before the agents framework starts a background task:
        # validation raised inside ``Model.stream_response`` is otherwise
        # captured into ``RunResultStreaming._stored_exception`` and may
        # be silently dropped if the queue completion sentinel is read
        # before the run_loop_task is observed as done.
        if (
            self.model_params.streaming_topic is None
            and self.model_params.stream_to_provider is None
        ):
            raise AgentsWorkflowError(
                "Runner.run_streamed requires "
                "ModelActivityParameters.streaming_topic or "
                "stream_to_provider to be set."
            )
        if self.model_params.use_local_activity:
            raise AgentsWorkflowError(
                "Runner.run_streamed is incompatible with "
                "use_local_activity (local activities do not support "
                "heartbeats or the workflow stream signal channel)."
            )

        converted_agent, injected_server = self._prepare_workflow_run(starting_agent, kwargs)

        streamed_result = self._runner.run_streamed(
            starting_agent=converted_agent,
            input=input,
            **kwargs,
        )

        # Mirror the AgentsException -> AgentsWorkflowError rewrap done
        # in run() above. The streaming runner attaches the actual run
        # to ``run_loop_task``; we wrap ``stream_events()`` (rather than
        # the task itself) so the rewrap happens on the consumer's
        # coroutine. Wrapping in a second asyncio task introduces a
        # scheduling gap: ``RunResultStreaming.stream_events()`` reads
        # the queue completion sentinel as soon as the run loop ends,
        # but the wrapper task only resumes its ``await`` after another
        # event-loop tick — between those two points, ``_check_errors``
        # sees no exception and ``_await_task_safely`` later swallows
        # the rewrapped one. Iterating the underlying generator first,
        # then inspecting the finished task on exit, keeps the rewrap
        # race-free without touching ``run_loop_task``.
        original_stream_events = streamed_result.stream_events
        run_loop_task = streamed_result.run_loop_task

        async def _stream_events_with_rewrap() -> AsyncIterator[Any]:
            try:
                try:
                    async for event in original_stream_events():
                        yield event
                except AgentsException as e:
                    _reraise_workflow_failure(e)
                    raise
                # The agents framework may have stored the run-loop
                # exception on ``_stored_exception`` (or surfaced it through
                # the iterator) without re-raising it through stream_events.
                # By the time the iterator is exhausted, ``run_loop_task``
                # is done — surface its exception here so a failed run
                # cannot appear successful, applying the workflow-failure
                # rewrap when applicable.
                if run_loop_task is not None and run_loop_task.done():
                    exc = run_loop_task.exception()
                    if exc is not None:
                        if isinstance(exc, AgentsException):
                            _reraise_workflow_failure(exc)
                        raise exc
            finally:
                # cleanup() is a no-op for the current _NexusTransportMCPServer (nothing to
                # actually release), but this stays symmetric with run()'s own cleanup call
                # and with whatever else _prepare_workflow_run ever injects here.
                if injected_server is not None:
                    await _cleanup_nexus_transport_mcp_server(injected_server)

        streamed_result.stream_events = _stream_events_with_rewrap  # type: ignore[method-assign]
        return streamed_result


def _reraise_workflow_failure(e: AgentsException) -> None:
    """Rewrap an AgentsException whose cause is a Temporal workflow failure.

    Returns normally when ``e`` is not workflow-failure-bearing so the
    caller can re-raise the original.
    """
    if e.__cause__ and workflow.is_failure_exception(e.__cause__):
        reraise = AgentsWorkflowError(
            f"Workflow failure exception in Agents Framework: {e}"
        )
        reraise.__traceback__ = e.__traceback__
        raise reraise from e.__cause__


def _model_name(agent: Agent[Any]) -> str | None:
    name = agent.model
    if name is not None and not isinstance(name, str):
        raise ValueError(
            "Temporal workflows require a model name to be a string in the agent."
        )
    return name
