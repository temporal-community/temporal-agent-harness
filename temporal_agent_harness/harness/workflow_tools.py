"""Durable messages between a harness agent and its registered subagents."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_workflow import _current_runner, tool_defn

_AGENT_MESSAGE_SIGNAL = "temporal_agent_harness.receive_message"


@dataclass(frozen=True)
class WorkflowMessage:
    """A durable message sent between agent Workflows."""

    id: str
    sender_workflow_id: str
    sender_run_id: str
    body: str


class AgentMessageInbox:
    """A deterministic inbox receiving messages through a Temporal Signal.

    Construct this object inside a Workflow to register its Signal handler.
    Messages can then be drained synchronously or awaited without polling an
    external system.
    """

    def __init__(self, *, signal_name: str = _AGENT_MESSAGE_SIGNAL) -> None:
        """Register an inbox for the current Workflow.

        Args:
            signal_name: Signal name used for message delivery. Most callers
                should use the shared default.
        """
        self._messages: list[WorkflowMessage] = []
        workflow.set_signal_handler(signal_name, self._receive)

    def _receive(self, message: WorkflowMessage) -> None:
        self._messages.append(message)

    @property
    def messages(self) -> tuple[WorkflowMessage, ...]:
        """Return a read-only snapshot of pending messages."""
        return tuple(self._messages)

    def drain(self) -> list[WorkflowMessage]:
        """Remove and return all pending messages in delivery order."""
        messages = self._messages
        self._messages = []
        return messages

    async def receive(
        self, *, timeout: timedelta | float | None = None
    ) -> WorkflowMessage:
        """Wait for and remove the next pending message.

        Args:
            timeout: Optional durable wait timeout.

        Returns:
            The next message in delivery order.
        """
        await workflow.wait_condition(
            lambda: bool(self._messages),
            timeout=timeout,
            timeout_summary="Waiting for an agent message",
        )
        return self._messages.pop(0)


def send_message_tool(
    *,
    tool_name: str = "send_message",
    tool_description: str | None = None,
    signal_name: str = _AGENT_MESSAGE_SIGNAL,
    inherently_safe: bool = False,
) -> Callable[..., Awaitable[str]]:
    """Create a shared tool for parent/subagent messages via Temporal Signals.

    Address an active child by the handle returned by ``subagent_toolset``'s
    start tool, or address the current workflow's parent as ``parent``.
    Recipient workflow IDs cannot be supplied by the model. A stopped or unknown
    child is rejected using the runner's existing subagent registry.

    Both agents must install ``AgentMessageInbox`` with the same Signal name.
    Acknowledgement confirms delivery, not processing or a reply. The recipient
    decides how to consume the message; Signals do not automatically start turns.
    The factory can be constructed outside a workflow; recipients are resolved
    during invocation through the current runner and workflow context.
    """

    async def send_message(recipient: str, message: str) -> str:
        info = workflow.info()
        if recipient == "parent":
            if info.parent is None:
                raise ApplicationError(
                    "This agent has no parent workflow to message",
                    type="invalid_message_recipient",
                    non_retryable=True,
                )
            target = info.parent.workflow_id
        else:
            target = _current_runner().subagent_workflow_id(recipient)
        envelope = WorkflowMessage(
            id=str(workflow.uuid4()),
            sender_workflow_id=info.workflow_id,
            sender_run_id=info.run_id,
            body=message,
        )
        await workflow.get_external_workflow_handle(target).signal(
            signal_name, envelope
        )
        return f"Message sent to {recipient}."

    send_message.__name__ = tool_name
    send_message.__doc__ = tool_description or (
        "Send a message to an active subagent using its handle from the start tool, "
        "or to your parent using recipient='parent'."
    )
    return tool_defn(inherently_safe=inherently_safe)(send_message)
