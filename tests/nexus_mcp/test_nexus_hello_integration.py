"""Exercise the example's synchronous, workflow-backed, and activity-backed tools."""

from __future__ import annotations

import asyncio
import re
import shutil
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import uvicorn
from nexus_mcp.durable_tools_gateway.generated import (
    CallToolInput,
    CallToolInputArguments,
    DeregisterInput,
    ListAgentEntriesInput,
    RegisterExternalInput,
    RegistryService,
)
from nexus_mcp.durable_tools_gateway.registry import (
    REGISTRY_WORKFLOW_ID,
    ToolRegistryWorkflow,
    fetch_external_tools,
)
from nexus_mcp.durable_tools_gateway.registry_service_handler import (
    RegistryServiceHandler,
    list_external_tools,
    mcp_proxy_activity,
)
from nexus_mcp.integrations.openai_agents import (
    NexusMCPServer,
    WorkflowNexusMCPServer,
)
from temporalio import workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from examples.nexus_hello.nexus_tool_service import (
    NEXUS_TASK_QUEUE,
    SERVICE_NAME,
    DelayedLuckyNumberWorkflow,
    DemoNexusToolsServiceHandler,
)
from examples.nexus_hello.tool_server import mcp


@workflow.defn(sandboxed=False)
class _NexusHelloCaller:
    @workflow.run
    async def run(self, endpoints: dict[str, str]) -> dict[str, Any]:
        native = WorkflowNexusMCPServer.for_service(SERVICE_NAME, endpoints["native"])
        tools = await native.list_tools()
        lucky = await native.call_tool(
            "demo-nexus_get_lucky_number", {"topic": "Temporal"}
        )
        delayed = await native.call_tool(
            "demo-nexus_get_delayed_lucky_number",
            {"topic": "Temporal", "delay_seconds": 0.01},
        )
        gateway = workflow.create_nexus_client(
            service=RegistryService, endpoint=endpoints["gateway"]
        )
        entries = await gateway.execute_operation(
            RegistryService.list_agent_entries,
            ListAgentEntriesInput(agent_id="NexusHelloAgent"),
        )
        fact = await gateway.execute_operation(
            RegistryService.call_tool,
            CallToolInput(
                agent_id="NexusHelloAgent",
                alias="demo",
                name="demo_get_fun_fact",
                arguments=CallToolInputArguments(
                    additional_properties={"topic": "Temporal"}
                ),
            ),
        )
        return {
            "tools": [tool.model_dump(by_alias=True) for tool in tools],
            "lucky": lucky.model_dump(by_alias=True),
            "delayed": delayed.model_dump(by_alias=True),
            "external_tools": {
                alias: [tool.additional_properties for tool in tools]
                for alias, tools in entries.remote_tools.additional_properties.items()
            },
            "fact": fact.result.additional_properties,
        }


@asynccontextmanager
async def _external_mcp_server():
    # Use the real example's server on a free port, leaving a running demo alone.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(
                mcp.streamable_http_app(stateless_http=True),
                log_level="error",
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        finally:
            server.should_exit = True
            await task


async def test_nexus_hello_tools_over_live_nexus() -> None:
    native_endpoint = f"hello-native-{uuid.uuid4()}"
    gateway_endpoint = f"hello-gateway-{uuid.uuid4()}"
    caller_queue = f"hello-caller-{uuid.uuid4()}"
    gateway_queue = f"hello-gateway-{uuid.uuid4()}"
    async with (
        _external_mcp_server() as external_url,
        await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter,
            dev_server_existing_path=shutil.which("temporal"),
            dev_server_extra_args=[
                "--dynamic-config-value",
                "history.enableUpdateCallbacks=true",
                "--dynamic-config-value",
                "nexusoperation.enableStandalone=true",
                "--dynamic-config-value",
                "activity.enableStandalone=true",
                "--dynamic-config-value",
                "activity.enableCallbacks=true",
                "--dynamic-config-value",
                'system.system.refreshNexusEndpointsMinWait="0s"',
            ],
        ) as env,
    ):
        await env.create_nexus_endpoint(native_endpoint, NEXUS_TASK_QUEUE)
        await env.create_nexus_endpoint(gateway_endpoint, gateway_queue)
        async with (
            Worker(
                env.client,
                task_queue=NEXUS_TASK_QUEUE,
                workflows=[DelayedLuckyNumberWorkflow],
                nexus_service_handlers=[DemoNexusToolsServiceHandler()],
            ),
            Worker(
                env.client,
                task_queue=gateway_queue,
                workflows=[ToolRegistryWorkflow],
                activities=[
                    fetch_external_tools,
                    list_external_tools,
                    mcp_proxy_activity,
                ],
                nexus_service_handlers=[RegistryServiceHandler()],
            ),
            Worker(env.client, task_queue=caller_queue, workflows=[_NexusHelloCaller]),
        ):
            registry = await env.client.start_workflow(
                ToolRegistryWorkflow.run,
                id=REGISTRY_WORKFLOW_ID,
                task_queue=gateway_queue,
            )
            try:
                gateway = env.client.create_nexus_client(
                    service=RegistryService, endpoint=gateway_endpoint
                )
                await gateway.execute_operation(
                    RegistryService.register_external,
                    RegisterExternalInput(
                        agent_id="NexusHelloAgent", name="demo", url=external_url
                    ),
                    id=str(uuid.uuid4()),
                )
                caller = await env.client.start_workflow(
                    _NexusHelloCaller.run,
                    {"native": native_endpoint, "gateway": gateway_endpoint},
                    id=str(uuid.uuid4()),
                    task_queue=caller_queue,
                    execution_timeout=timedelta(seconds=60),
                )
                result = await caller.result()
                tools = {tool["name"]: tool for tool in result["tools"]}
                assert set(tools) == {
                    "demo-nexus_get_lucky_number",
                    "demo-nexus_get_delayed_lucky_number",
                }
                delayed_tool = tools["demo-nexus_get_delayed_lucky_number"]
                assert delayed_tool["inputSchema"]["required"] == ["topic"]
                assert delayed_tool["outputSchema"]["required"] == ["message"]
                assert delayed_tool["annotations"]["readOnlyHint"] is True
                assert result["lucky"]["isError"] is False
                assert re.fullmatch(
                    r"Temporal's lucky number today is (?:[1-9]\d?|100)\.",
                    result["lucky"]["content"][0]["text"],
                )
                assert result["delayed"]["isError"] is False
                assert re.fullmatch(
                    r"Temporal's delayed lucky number is (?:[1-9]\d?|100)\.",
                    result["delayed"]["structuredContent"]["message"],
                )
                assert (
                    result["external_tools"]["demo"][0]["name"] == "demo_get_fun_fact"
                )
                assert result["fact"]["isError"] is False
                assert "Temporal" in result["fact"]["content"][0]["text"]

                # Only the durable calls should produce NexusOperationStarted.
                history = await caller.fetch_history()
                scheduled = {
                    event.event_id: event.nexus_operation_scheduled_event_attributes.operation
                    for event in history.events
                    if event.HasField("nexus_operation_scheduled_event_attributes")
                }
                assert sorted(
                    scheduled[
                        event.nexus_operation_started_event_attributes.scheduled_event_id
                    ]
                    for event in history.events
                    if event.HasField("nexus_operation_started_event_attributes")
                ) == ["ListAgentEntries", "call_tool", "get_delayed_lucky_number"]

                standalone = NexusMCPServer.for_service(
                    env.client, SERVICE_NAME, native_endpoint
                )
                delayed = await standalone.call_tool(
                    "demo-nexus_get_delayed_lucky_number",
                    {"topic": "Standalone", "delay_seconds": 0.01},
                )
                assert not delayed.is_error
                assert delayed.structured_content["message"].startswith(
                    "Standalone's delayed"
                )

                await gateway.execute_operation(
                    RegistryService.deregister,
                    DeregisterInput(agent_id="NexusHelloAgent", name="demo"),
                    id=str(uuid.uuid4()),
                )
                empty = await gateway.execute_operation(
                    RegistryService.list_agent_entries,
                    ListAgentEntriesInput(agent_id="NexusHelloAgent"),
                    id=str(uuid.uuid4()),
                )
                assert empty.remote_tools.additional_properties == {}
            finally:
                await registry.cancel()
