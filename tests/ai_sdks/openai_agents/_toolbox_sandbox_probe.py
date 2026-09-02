"""Workflow used to prove A2A toolbox materialization is sandbox-safe."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from durable_tools_gateway.resources import ResourceDescriptor, text_agent_card

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
                )
            ],
            account_id="account-1",
            gateway_name="RegistryService",
            gateway_endpoint="registry-endpoint",
            version="1",
        )
        return [tool.__name__ for tool in toolbox.subagent_tools]
