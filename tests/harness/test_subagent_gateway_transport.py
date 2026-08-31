from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from durable_tools_gateway.generated import (
    DispatchSubagentTurnOutput,
    StartSubagentOutput,
)

from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporal_agent_harness.harness.stream_context import TurnStreamContext
from temporal_agent_harness.harness.subagent_gateway_transport import GatewayTransport


@patch(
    "temporal_agent_harness.harness.subagent_gateway_transport.workflow.create_nexus_client"
)
async def test_gateway_transport_uses_started_instance(mock_create_client: MagicMock) -> None:
    client = MagicMock()
    client.execute_operation = AsyncMock(
        side_effect=[
            StartSubagentOutput(instance_id="instance-1"),
            DispatchSubagentTurnOutput(
                output='{"text":"done"}', turn_id="turn-1", turn_number=1
            ),
            None,
        ]
    )
    mock_create_client.return_value = client
    transport = GatewayTransport("agent-1", "writer")

    instance_id = await transport.start(agent_key="writer", config=AgentConfig())
    with patch(
        "temporal_agent_harness.harness.subagent_gateway_transport._current_runner"
    ) as current_runner:
        current_runner.return_value.publish = MagicMock()
        result = await transport.dispatch(
            target=instance_id,
            msg_type="ask",
            payload={"text": "hello"},
            expected_turn=1,
            from_offset=0,
            handle="parent-child",
            agent_key="writer",
            parent_stream_context=TurnStreamContext(
                agent_id="agent-1", turn_id="parent-turn", turn_number=1
            ),
        )
    await transport.stop(target=instance_id)

    start_input = client.execute_operation.await_args_list[0].args[1]
    dispatch_input = client.execute_operation.await_args_list[1].args[1]
    stop_input = client.execute_operation.await_args_list[2].args[1]
    assert start_input.alias == "writer"
    assert dispatch_input.instance_id == "instance-1"
    assert stop_input.instance_id == "instance-1"
    assert result.output == {"text": "done"}
