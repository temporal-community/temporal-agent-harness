import nexusrpc
from nexus_a2a import A2A_SERVICE_NAME, A2AService, SubscribeToTaskInput


def test_service_uses_standard_a2a_operation_names() -> None:
    assert nexusrpc.get_service_definition(A2AService).name == A2A_SERVICE_NAME
    assert A2AService.send_message.name == "SendMessage"
    assert A2AService.subscribe_to_task.name == "SubscribeToTask"


def test_subscription_input_defaults_to_first_cursor() -> None:
    request = SubscribeToTaskInput(id="task-1")
    assert request.cursor == 0
    assert request.timeout_seconds == 30.0
