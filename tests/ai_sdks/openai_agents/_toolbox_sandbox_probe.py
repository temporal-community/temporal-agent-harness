"""Workflow used to prove A2A toolbox materialization is sandbox-safe."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from nexus_mcp.durable_tools_gateway.resources import (
        ResourceDescriptor,
        text_agent_card,
    )

    from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import (
        _materialize_toolbox,
    )


@workflow.defn
class ToolboxSandboxProbe:
    @workflow.run
    async def run(self) -> list[str]:
        toolbox = _materialize_toolbox(
            [
                ResourceDescriptor(
                    resource_id="research",
                    revision=1,
                    category="agent",
                    transport="nexus",
                    label="Research",
                    description="Sandbox probe",
                    endpoint="research-endpoint",
                    service="A2AService",
                    agent_card=text_agent_card(
                        name="Research",
                        description="Sandbox probe",
                        endpoint="research-endpoint",
                        transport="nexus",
                    ),
                ),
                ResourceDescriptor(
                    resource_id="native-tools",
                    revision=1,
                    category="mcp",
                    transport="nexus",
                    label="Native tools",
                    description="Sandbox probe",
                    endpoint="tools-endpoint",
                    service="tools-service",
                ),
                ResourceDescriptor(
                    resource_id="remote-tools",
                    revision=1,
                    category="mcp",
                    transport="external_http",
                    label="Remote tools",
                    description="Sandbox probe",
                    endpoint="http://tools/mcp",
                ),
            ],
            account_id="account-1",
            gateway_name="RegistryService",
            gateway_endpoint="registry-endpoint",
            version="1",
        )
        return [
            *(server.name for server in toolbox.mcp_servers),
            *(tool.__name__ for tool in toolbox.subagent_tools),
        ]
