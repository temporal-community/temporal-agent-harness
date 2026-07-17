# ABOUTME: Workflow definitions for test_nexus_mcp.py, kept in their own minimal module.
# Temporal's sandboxed workflow runner re-execs a workflow's ENTIRE defining module to
# validate it — so a file that also carries pytest imports at module scope (as
# test_nexus_mcp.py does, for its non-workflow unit tests) cannot also define a
# @workflow.defn class without the sandbox tripping over that unrelated import. Keeping
# these workflows here, with nothing but the imports they actually need (agents/openai are
# fine — OpenAIAgentsPlugin's own workflow_runner hook marks them sandbox passthrough), is
# what lets us exercise BOTH the sandboxed=False success path and the default-sandboxed
# failure path without that cross-contamination.

from __future__ import annotations

from datetime import timedelta

from agents import Agent, Runner
from durable_tools_gateway import REGISTRY_SERVICE_NAME
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

from temporal_agent_harness.ai_sdks.openai_agents._model_parameters import ModelActivityParameters
from temporal_agent_harness.ai_sdks.openai_agents._openai_runner import TemporalOpenAIRunner
from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
    nexus_mcp_server_registry,
    nexus_transport_mcp_server,
)
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import AgentConfig, ToolApprovalPolicy
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

# Must match the Nexus Endpoint the test fixture actually creates (targeting the gateway
# worker's task queue) — WorkflowTransport reaches RegistryService.list_tools/call_tool via a
# real Nexus call, not an activity, so this has to resolve to a real endpoint resource. The
# gateway is registered against each probe workflow the exact same "register_mcp_server"
# signal any other Nexus service uses, under its own real Nexus service name
# (durable_tools_gateway.REGISTRY_SERVICE_NAME) — there is no separate worker-level gateway
# config, and nothing else (like a "role") to declare at registration time; WorkflowTransport
# tells direct servers and proxies apart structurally, from what list_tools returns.
GATEWAY_ENDPOINT = "pytest-gateway-endpoint"
NEXUS_NATIVE_SERVICE_NAME = "probe-nexus"


@workflow.defn(sandboxed=False)
class NexusTransportProbe:
    """Opens the Nexus-transport MCP server (the manual/explicit path — see
    AutoInjectionProbe for the plugin-level automatic path) and round-trips
    list_tools/call_tool — exercising the exact MCPServer contract the OpenAI Agents SDK
    itself calls.

    Waits for the Nexus-native server to have registered against this workflow's
    NexusMcpServerRegistry before proceeding, so the test can start this workflow, signal
    "register_mcp_server" against it (exactly as an external Nexus-native server's own worker
    would, and exactly the same signal the gateway itself registers with — there is no
    separate registration mechanism for it, or anything role-like to declare) and only then
    expect list_tools/call_tool to see both.
    """

    @workflow.run
    async def run(self, text: str) -> dict:
        # Forces the registry (and its signal handlers) to exist before waiting on it —
        # not strictly required for correctness (signals to a not-yet-registered handler
        # name are buffered by the server until one is registered, confirmed live), but
        # makes the wait_condition below meaningful from the start.
        registry = nexus_mcp_server_registry()
        # The test signals the gateway's registration first, so by the time this unblocks
        # both are already applied (same workflow, signals processed in receipt order).
        await workflow.wait_condition(
            lambda: NEXUS_NATIVE_SERVICE_NAME in registry.servers
        )
        # enabled_services=None: this probe tests WorkflowTransport's routing mechanics
        # (direct-Nexus vs. proxy-fallback vs. unknown-name), predating the opt-in
        # allowlist feature -- disable it here rather than entangle the two concerns. See
        # test_opt_in_gates_tool_visibility for the allowlist itself.
        async with nexus_transport_mcp_server(
            name="probe", enabled_services=None
        ) as mcp_server:
            tools = await mcp_server.list_tools()
            gateway_result = await mcp_server.call_tool("demo_echo_ping", {"text": text})
            nexus_native_result = await mcp_server.call_tool(
                "probe-nexus_ping", {"text": text}
            )
            missing = await mcp_server.call_tool("nope_missing", {"text": text})
            return {
                "tool_names": sorted(t.name for t in tools),
                "gateway_result_text": (
                    gateway_result.content[0].text if gateway_result.content else None
                ),
                "gateway_is_error": gateway_result.isError,
                "nexus_native_result_text": (
                    nexus_native_result.content[0].text
                    if nexus_native_result.content
                    else None
                ),
                "nexus_native_is_error": nexus_native_result.isError,
                "missing_is_error": missing.isError,
            }


@workflow.defn
class SandboxedNexusTransportProbe:
    """Deliberately left at the default (sandboxed) setting to prove
    nexus_transport_mcp_server fails fast with a clear error instead of a cryptic
    RestrictedWorkflowAccessError from deep inside anyio."""

    @workflow.run
    async def run(self) -> None:
        async with nexus_transport_mcp_server():
            pass


@workflow.defn(sandboxed=False)
class AutoInjectionProbe:
    """Exercises OpenAIAgentsPlugin's automatic MCP-server injection
    (nexus_transport=True) directly against TemporalOpenAIRunner, without needing a
    real model call: _prepare_workflow_run is the exact mutation Runner.run()/run_streamed()
    apply to every agent before executing it, so calling it directly and inspecting the
    result proves the injection without any OpenAI API access.

    Calls _prepare_workflow_run (and cleans up its injected server afterward) directly,
    bypassing run()/run_streamed(), since this probe has no real model to call -- but this
    means IT is responsible for the same cleanup run()/run_streamed() normally do
    automatically. That cleanup is a no-op for the current _NexusTransportMCPServer (see
    _nexus_mcp.py's module docstring for why it no longer holds a session/router task at
    all), but calling it anyway keeps this probe's shape matching run()/run_streamed()'s own
    contract -- see _openai_runner.py's comment on _create_nexus_transport_mcp_server for the
    historical eviction-hang reason that contract exists.
    """

    @workflow.run
    async def run(self) -> dict:
        from temporal_agent_harness.ai_sdks.openai_agents._openai_runner import (
            _cleanup_nexus_transport_mcp_server,
        )

        runner = TemporalOpenAIRunner(
            ModelActivityParameters(start_to_close_timeout=timedelta(seconds=30)),
            nexus_transport=True,
        )

        # No mcp_servers=[...] at the call site at all -- this is the entire point.
        plain_agent = Agent(name="probe", instructions="probe")
        converted, injected_1 = runner._prepare_workflow_run(plain_agent, {})
        assert injected_1 is not None
        try:
            # list_tools() alone is enough to prove the injected server is real and
            # functional -- NOT asserting on a call_tool() round trip here deliberately:
            # that's already covered, for both the direct and gateway-routed paths, by
            # test_agent_runner_calls_both_nexus_native_and_gateway_tools; irrelevant to
            # what THIS test is proving (the injection mechanism itself).
            [server] = converted.mcp_servers
            tools = await server.list_tools()
        finally:
            await _cleanup_nexus_transport_mcp_server(injected_1)

        # A second, independent Runner.run()-shaped call (simulating a 2nd conversation
        # turn) gets its OWN fresh transport, not the first call's -- see
        # _create_nexus_transport_mcp_server's comment for the historical (no longer
        # applicable, but still harmless) reason this class is constructed fresh per call.
        _converted_again, injected_2 = runner._prepare_workflow_run(
            Agent(name="probe2", instructions="x"), {}
        )
        assert injected_2 is not None
        try:
            distinct_instances = injected_1 is not injected_2
        finally:
            await _cleanup_nexus_transport_mcp_server(injected_2)

        return {
            "injected_count": len(converted.mcp_servers),
            "distinct_instances_per_call": distinct_instances,
            "tool_names": sorted(t.name for t in tools),
        }


@workflow.defn(sandboxed=False)
@agent.defn
class OptInEnforcementProbe:
    """A REAL AgentWorkflowRunner-backed agent (the standardized AgentConfig-in,
    @agent.defn shape every harness agent uses) -- proving nexus_transport_mcp_server's
    DEFAULT (no explicit enabled_services override) actually enforces
    AgentWorkflowRunner.enabled_mcp_servers, and that widening it at runtime via the
    set_enabled_mcp_servers update takes effect immediately, mid-conversation.

    Never calls self._runner.run(self) -- no turn loop, no @agent.accepts handlers needed;
    constructing the runner (for its config-resolution side effect) is all this needs.
    """

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # Explicit barrier the test polls before sending the widening update -- an update
        # handler runs concurrently with the main coroutine (interleaved at ITS `await`
        # points, e.g. the ones inside phase 1 below), so sending it "after" phase 1's own
        # signal/await from the TEST's perspective does NOT guarantee phase 1 has actually
        # finished inside the workflow. Confirmed live: without this barrier, the update
        # sometimes lands mid-phase-1, making "probe-nexus_ping" spuriously visible early.
        self._phase1_done = False

    @workflow.query
    def phase1_done(self) -> bool:
        return self._phase1_done

    @workflow.run
    async def run(self, _config: AgentConfig) -> dict:
        registry = nexus_mcp_server_registry()
        # The test signals the gateway's registration first, so by the time this unblocks
        # both are already applied (same workflow, signals processed in receipt order).
        await workflow.wait_condition(
            lambda: NEXUS_NATIVE_SERVICE_NAME in registry.servers
        )

        # No enabled_services override -- relies entirely on nexus_transport_mcp_server's
        # default, which reads THIS workflow's AgentWorkflowRunner.enabled_mcp_servers live.
        async with nexus_transport_mcp_server(name="probe") as mcp_server:
            tools_before = await mcp_server.list_tools()
            # NOT calling "demo_echo_ping" here -- irrelevant to what THIS test proves
            # (tool_names_before already proves "demo" is visible/enabled; the gateway
            # call_tool round trip itself is covered by
            # test_agent_runner_calls_both_nexus_native_and_gateway_tools). This test is
            # about enabled_mcp_servers gating: nexus_native_result_before proves call_tool
            # rejects a NOT-yet-enabled service.
            nexus_native_result_before = await mcp_server.call_tool(
                "probe-nexus_ping", {"text": "hi"}
            )
        self._phase1_done = True

        # Widened at runtime via a REAL update (see test_opt_in_gates_tool_visibility) --
        # wait for it to land before opening the second transport.
        await workflow.wait_condition(
            lambda: "probe-nexus" in self._runner.enabled_mcp_servers
        )
        async with nexus_transport_mcp_server(name="probe2") as mcp_server2:
            tools_after = await mcp_server2.list_tools()
            nexus_native_result_after = await mcp_server2.call_tool(
                "probe-nexus_ping", {"text": "hi"}
            )

        return {
            "tool_names_before": sorted(t.name for t in tools_before),
            "nexus_native_is_error_before": nexus_native_result_before.isError,
            "tool_names_after": sorted(t.name for t in tools_after),
            "nexus_native_is_error_after": nexus_native_result_after.isError,
        }


@workflow.defn(sandboxed=False)
@agent.defn
class AgentToolCallE2EProbe:
    """End-to-end probe: a REAL ``Agent``, built with no ``mcp_servers=[...]`` at all
    (auto-injected via ``TemporalOpenAIRunner``), actually driven through
    ``Runner.run_streamed()`` — the exact call examples/nexus_hello's own ``ask()`` handler
    makes — against a fake streaming model that requests a 1st-party Nexus-native tool call,
    then a gateway-routed 3rd-party tool call, then a final message. This is the exact "list
    tools, use each one" shape that triggered a live deadlock (TMPRL1101) in that example,
    which none of this file's OTHER probes exercise: they all call
    ``_NexusTransportMCPServer.list_tools``/``call_tool`` directly, bypassing ``Runner`` (and
    the OpenAI Agents SDK's own tracing/tool-orchestration/streaming machinery around it)
    entirely.
    """

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            # The allowlist filters by each TOOL's own prefix, not the registered name of
            # whatever answers for it -- "demo" (the fake 3rd-party tool's own prefix), not
            # REGISTRY_SERVICE_NAME (the proxy's registration name that fronts it).
            enabled_mcp_servers_default=["demo", NEXUS_NATIVE_SERVICE_NAME],
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> dict:
        # Same self-serve registration shape as the real example: the gateway registers
        # itself under its own real Nexus service name; the Nexus-native service is
        # registered live, externally, via the same "register_mcp_server" signal.
        registry = nexus_mcp_server_registry()
        registry.register(REGISTRY_SERVICE_NAME, GATEWAY_ENDPOINT)
        await workflow.wait_condition(
            lambda: NEXUS_NATIVE_SERVICE_NAME in registry.servers
        )

        probe_agent = Agent(name="probe", instructions="probe", model="test-model")
        result = Runner.run_streamed(
            probe_agent, input="list your tools and use each one"
        )
        async for _event in result.stream_events():
            pass

        # Surfaced (not just the final message) so a caller can tell a tool call that
        # actually FAILED (e.g. a torn-down shared session under concurrent calls) apart from
        # one that succeeded -- final_output alone can't: this probe's fake model always
        # returns the same scripted final message regardless of what the tool calls before it
        # actually returned.
        tool_outputs = {
            item["call_id"]: item["output"]
            for item in result.to_input_list()
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        }
        return {"final_output": str(result.final_output), "tool_outputs": tool_outputs}
