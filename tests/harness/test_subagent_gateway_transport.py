from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from a2a.types import Artifact, Part, SendMessageResponse, Task

from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporal_agent_harness.harness.stream_context import TurnStreamContext
from temporal_agent_harness.harness.subagent_gateway_transport import GatewayTransport


@patch(
    "temporal_agent_harness.harness.subagent_gateway_transport.workflow.create_nexus_client"
)
async def test_gateway_transport_uses_started_instance(
    mock_create_client: MagicMock,
) -> None:
    client = MagicMock()
    client.execute_operation = AsyncMock(
        side_effect=[
            SendMessageResponse(
                task=Task(
                    id="writer-subagent-instance-1",
                    artifacts=[
                        Artifact(artifact_id="turn-1", parts=[Part(text="done")])
                    ],
                    metadata={
                        "temporal.io/turn-number": 1,
                        "temporal.io/turn-id": "turn-1",
                    },
                )
            ),
            Task(id="writer-subagent-instance-1"),
        ]
    )
    mock_create_client.return_value = client
    transport = GatewayTransport("agent-1", "writer")

    with patch(
        "temporal_agent_harness.harness.subagent_gateway_transport.workflow.uuid4",
        return_value="instance-1",
    ):
        instance_id = await transport.start(agent_key="writer", config=AgentConfig())
    with (
        patch(
            "temporal_agent_harness.harness.subagent_gateway_transport._current_runner"
        ) as current_runner,
        patch(
            "temporal_agent_harness.harness.subagent_gateway_transport.workflow.uuid4",
            return_value="message-1",
        ),
    ):
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

    dispatch_input = client.execute_operation.await_args_list[0].args[1]
    stop_input = client.execute_operation.await_args_list[1].args[1]
    assert dispatch_input.message.task_id == "writer-subagent-instance-1"
    assert stop_input.id == "writer-subagent-instance-1"
    assert result.output == {"text": "done"}
