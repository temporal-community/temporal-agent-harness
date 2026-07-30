# ABOUTME: Workflow definitions for test_nexus_mcp.py, kept in their own minimal module.
# Temporal's sandboxed workflow runner re-execs a workflow's ENTIRE defining module to
# validate it — so a file that also carries pytest imports at module scope (as
# test_nexus_mcp.py does, for its non-workflow unit tests) cannot also define a
# @workflow.defn class without the sandbox tripping over that unrelated import. Keeping
# these workflows here, with nothing but the imports they actually need (agents/openai are
# fine — OpenAIAgentsPlugin's own workflow_runner hook marks them sandbox passthrough),
# avoids that cross-contamination.

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from agents import Agent, Runner
from durable_tools_gateway import REGISTRY_SERVICE_NAME
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from transport.workflow_transport import WorkflowTransport

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
        # Forces the registry (and its signal handlers) to exist before waiting on it --
        # not strictly required for correctness (signals to a not-yet-registered handler
        # name are buffered by the server until one is registered, confirmed live), but
        # makes the wait_condition below meaningful from the start.
        registry = nexus_mcp_server_registry()
        # The test signals the gateway's registration first, so by the time this unblocks
        # both are already applied (same workflow, signals processed in receipt order).
        await workflow.wait_condition(
            lambda: NEXUS_NATIVE_SERVICE_NAME in registry.servers
        )
        async with nexus_transport_mcp_server(name="probe") as mcp_server:
            tools = await mcp_server.list_tools()
            gateway_result = await mcp_server.call_tool("demo_echo_ping", {"text": text})
            nexus_native_result = await mcp_server.call_tool(
                "probe-nexus_ping", {"text": text}
            )
            # Not auto-listed (hand-authored, not @nexus_mcp_tool -- see
            # ProbeNexusToolsServiceHandler.structured_ping), but still directly callable:
            # proves a Nexus-native tool can opt into structured content and have
            # WorkflowTransport preserve it untouched, the direct-call counterpart to the
            # gateway-proxied path's own structuredContent round trip (tested separately).
            structured_result = await mcp_server.call_tool(
                "probe-nexus_structured_ping", {"text": text}
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
                "nexus_native_structured_content": structured_result.structuredContent,
                "missing_is_error": missing.isError,
            }


@workflow.defn(sandboxed=False)
class BareWorkflowTransportProbe:
    """Exercises WorkflowTransport directly -- no agents.mcp.MCPServer, no
    _NexusTransportMCPServer, no OpenAIAgentsPlugin. Proves list_tools/call_tool/name work
    standalone. (list_prompts/get_prompt are OpenAI-Agents-SDK-specific and live on
    _NexusTransportMCPServer only -- WorkflowTransport doesn't have them.)
    """

    @workflow.run
    async def run(self, text: str) -> dict:
        registry = nexus_mcp_server_registry()
        await workflow.wait_condition(
            lambda: NEXUS_NATIVE_SERVICE_NAME in registry.servers
        )
        transport = WorkflowTransport(registry.servers, name="bare-probe")
        tools = await transport.list_tools()
        gateway_result = await transport.call_tool("demo_echo_ping", {"text": text})
        return {
            "name": transport.name,
            "tool_names": sorted(t.name for t in tools),
            "gateway_is_error": gateway_result.isError,
            "gateway_result_text": (
                gateway_result.content[0].text if gateway_result.content else None
            ),
            # Proves the CallToolResult content-model round trip: structuredContent must
            # survive activity -> workflow -> Nexus -> WorkflowTransport intact, not get
            # silently dropped the way a flattened-to-text result would.
            "gateway_structured_content": gateway_result.structuredContent,
        }


@workflow.defn(sandboxed=False)
class ScopedTransportProbe:
    """A WorkflowTransport scoped (via allowed_servers) to ONLY the Nexus-native service,
    even though the gateway is ALSO registered against this same workflow's shared
    registry -- proves allowed_servers actually narrows visibility rather than every
    transport instance seeing everything registered."""

    @workflow.run
    async def run(self, text: str) -> dict:
        registry = nexus_mcp_server_registry()
        await workflow.wait_condition(
            lambda: NEXUS_NATIVE_SERVICE_NAME in registry.servers
            and REGISTRY_SERVICE_NAME in registry.servers
        )
        transport = WorkflowTransport(
            registry.servers,
            name="scoped-probe",
            allowed_servers=frozenset({NEXUS_NATIVE_SERVICE_NAME}),
        )
        tools = await transport.list_tools()
        nexus_native_result = await transport.call_tool(
            "probe-nexus_ping", {"text": text}
        )
        gateway_result = await transport.call_tool("demo_echo_ping", {"text": text})
        return {
            "tool_names": sorted(t.name for t in tools),
            "nexus_native_is_error": nexus_native_result.isError,
            "gateway_is_error": gateway_result.isError,
        }


@workflow.defn(sandboxed=False)
class RegisterExternalViaNexusProbe:
    """Calls RegistryService.register_external directly over Nexus -- the real
    registration path (name validation, then a durable fetch via RegisterExternalWorkflow,
    with a failure surfaced back to the caller synchronously), as opposed to the
    register_external SIGNAL other probes use to seed the gateway directly, skipping that
    validation/fetch entirely."""

    @workflow.run
    async def run(self, input: dict) -> None:
        client = workflow.create_nexus_client(
            service=REGISTRY_SERVICE_NAME, endpoint=GATEWAY_ENDPOINT
        )
        await client.execute_operation("RegisterExternal", input)


@workflow.defn(sandboxed=False)
class RegistryOnlyProbe:
    """Just the registry, running forever -- lets a test freely signal
    register_mcp_server/deregister_mcp_server and query list_registered_mcp_servers at
    will, with no gateway/Nexus-native service or turn machinery to also stand up."""

    @workflow.run
    async def run(self) -> None:
        nexus_mcp_server_registry()
        await workflow.wait_condition(lambda: False)


@workflow.defn(sandboxed=False)
class DefaultServersProbe:
    """Seeds NexusMcpServerRegistry with default_servers -- a known, fixed tool set
    declared at workflow-definition time, needing no live register_mcp_server signal at
    all for the common case where the tool set is already known."""

    @workflow.run
    async def run(self, nexus_native_endpoint: str) -> dict:
        registry = nexus_mcp_server_registry(
            default_servers={NEXUS_NATIVE_SERVICE_NAME: nexus_native_endpoint}
        )
        async with nexus_transport_mcp_server(name="probe") as mcp_server:
            tools = await mcp_server.list_tools()
            result = await mcp_server.call_tool("probe-nexus_ping", {"text": "hi"})
            return {
                "registered_servers": dict(registry.servers),
                "tool_names": sorted(t.name for t in tools),
                "result_text": result.content[0].text if result.content else None,
            }


@workflow.defn(sandboxed=False)
class AutoInjectionProbe:
    """Exercises OpenAIAgentsPlugin's automatic MCP-server injection
    (nexus_mcp_initial_servers={}) directly against TemporalOpenAIRunner, without needing a
    real model call: _prepare_workflow_run is the exact mutation Runner.run()/run_streamed()
    apply to every agent before executing it, so calling it directly and inspecting the
    result proves the injection without any OpenAI API access.

    Calls _prepare_workflow_run directly, bypassing run()/run_streamed(), since this probe
    has no real model to call. No cleanup needed -- the injected _NexusTransportMCPServer
    holds no session/resources to release.
    """

    @workflow.run
    async def run(self) -> dict:
        runner = TemporalOpenAIRunner(
            ModelActivityParameters(start_to_close_timeout=timedelta(seconds=30)),
            nexus_mcp_initial_servers={},
        )

        # No mcp_servers=[...] at the call site at all -- this is the entire point.
        plain_agent = Agent(name="probe", instructions="probe")
        converted = runner._prepare_workflow_run(plain_agent, {})
        # list_tools() alone is enough to prove the injected server is real and
        # functional -- NOT asserting on a call_tool() round trip here deliberately:
        # that's already covered, for both the direct and gateway-routed paths, by
        # test_agent_runner_calls_both_nexus_native_and_gateway_tools; irrelevant to
        # what THIS test is proving (the injection mechanism itself).
        [server] = converted.mcp_servers
        tools = await server.list_tools()

        # A second, independent Runner.run()-shaped call (simulating a 2nd conversation
        # turn) gets its OWN fresh transport, not the first call's.
        converted_again = runner._prepare_workflow_run(
            Agent(name="probe2", instructions="x"), {}
        )
        [server_2] = converted_again.mcp_servers

        return {
            "injected_count": len(converted.mcp_servers),
            "distinct_instances_per_call": server is not server_2,
            "tool_names": sorted(t.name for t in tools),
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
        # context= is what triggers the approval-gate wrapping (matches ask()'s real usage).
        result = Runner.run_streamed(
            probe_agent, input="list your tools and use each one", context=self._runner
        )
        async for _event in result.stream_events():
            pass

        # Surfaced (not just the final message) so a caller can tell a tool call that
        # actually FAILED (e.g. a torn-down shared session under concurrent calls) apart from
        # one that succeeded -- final_output alone can't: this probe's fake model always
        # returns the same scripted final message regardless of what the tool calls before it
        # actually returned.
        tool_outputs = {
            item["call_id"]: item.get("output")
            for raw_item in result.to_input_list()
            if isinstance(raw_item, dict)
            and (item := cast(dict[str, Any], raw_item)).get("type")
            == "function_call_output"
        }
        return {"final_output": str(result.final_output), "tool_outputs": tool_outputs}
