"""SDK independent child workflow tools and durable agent messaging."""

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, get_type_hints

from temporalio import workflow
from temporalio.common import Priority, RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.workflow import (
    ChildWorkflowCancellationType,
    ParentClosePolicy,
    VersioningIntent,
)

from temporal_agent_harness.harness.agent_workflow import tool_defn

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


def child_workflow_as_tool(
    fn: Callable[..., Any],
    *,
    tool_name: str | None = None,
    tool_description: str | None = None,
    id: str | None = None,
    task_queue: str | None = None,
    cancellation_type: ChildWorkflowCancellationType = (
        ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED
    ),
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    execution_timeout: timedelta | None = None,
    run_timeout: timedelta | None = None,
    task_timeout: timedelta | None = None,
    retry_policy: RetryPolicy | None = None,
    id_reuse_policy: WorkflowIDReusePolicy = WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    versioning_intent: VersioningIntent | None = None,
    static_summary: str | None = None,
    static_details: str | None = None,
    priority: Priority = Priority.default,
    inherently_safe: bool = False,
) -> Callable[..., Awaitable[Any]]:
    """Expose a decorated Workflow run method as a shared harness tool.

    The model sees the run method's typed parameters, without ``self``. Invocation
    waits for the child result, preserving its type. The child may use any inner
    harness, or no agent SDK. Register it on the selected task queue's worker.

    Leave ``id`` unset for repeated calls. Temporal assigns deterministic unique
    IDs. Lifecycle options are forwarded to ``execute_child_workflow``. Approval
    policy and tool events are supplied by ``tool_defn`` like other harness tools.
    """
    definition = workflow._Definition.from_run_fn(fn)
    if definition is None or definition.name is None:
        raise TypeError("Input must be a decorated Workflow run method")
    hints = get_type_hints(fn)
    original = inspect.signature(fn)
    parameters = list(original.parameters.values())
    if parameters and parameters[0].name == "self":
        parameters = parameters[1:]
    if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in parameters):
        raise TypeError("Workflow tool parameters must have explicit names and types")
    model_signature = original.replace(
        parameters=[
            p.replace(annotation=hints.get(p.name, p.annotation)) for p in parameters
        ],
        return_annotation=hints.get("return", original.return_annotation),
    )

    async def run_child(*args: Any, **kwargs: Any) -> Any:
        bound = model_signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return await workflow.execute_child_workflow(
            definition.name,
            args=list(bound.arguments.values()),
            id=id,
            task_queue=task_queue,
            result_type=definition.ret_type,
            cancellation_type=cancellation_type,
            parent_close_policy=parent_close_policy,
            execution_timeout=execution_timeout,
            run_timeout=run_timeout,
            task_timeout=task_timeout,
            retry_policy=retry_policy,
            id_reuse_policy=id_reuse_policy,
            versioning_intent=versioning_intent,
            static_summary=static_summary or tool_description or fn.__doc__,
            static_details=static_details,
            priority=priority,
        )

    run_child.__name__ = tool_name or definition.name
    run_child.__doc__ = tool_description or fn.__doc__ or "Execute a child workflow."
    run_child.__signature__ = model_signature  # type: ignore[attr-defined]
    run_child.__annotations__ = {
        p.name: p.annotation for p in model_signature.parameters.values()
    }
    run_child.__annotations__["return"] = model_signature.return_annotation
    return tool_defn(inherently_safe=inherently_safe)(run_child)


def send_message_tool(
    recipients: Mapping[str, str],
    *,
    include_parent: bool = False,
    tool_name: str = "send_message",
    tool_description: str | None = None,
    signal_name: str = _AGENT_MESSAGE_SIGNAL,
    inherently_safe: bool = False,
) -> Callable[..., Awaitable[str]]:
    """Build a shared harness tool for asynchronous workflow messaging.

    The model chooses an alias from ``recipients``, never a raw workflow ID.
    ``include_parent`` adds ``parent`` at invocation time, so factories can be
    constructed outside a workflow. Recipients must register AgentMessageInbox
    with the same Signal name. Delivery acknowledgement does not mean the
    recipient has processed the message or produced a reply.
    """
    allowed = dict(recipients)

    async def send_message(recipient: str, message: str) -> str:
        resolved = dict(allowed)
        info = workflow.info()
        if include_parent:
            if info.parent is None:
                raise ApplicationError(
                    "include_parent=True requires a Child Workflow",
                    type="invalid_tool",
                    non_retryable=True,
                )
            resolved["parent"] = info.parent.workflow_id
        target = resolved.get(recipient)
        if target is None:
            raise ApplicationError(
                f"Unknown message recipient {recipient!r}; allowed recipients: "
                + (", ".join(sorted(resolved)) or "none"),
                type="invalid_message_recipient",
                non_retryable=True,
            )
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

    aliases = sorted(set(allowed) | ({"parent"} if include_parent else set()))
    send_message.__name__ = tool_name
    send_message.__doc__ = tool_description or (
        "Send a message to another agent. Allowed recipients: "
        + (", ".join(aliases) or "none")
        + "."
    )
    return tool_defn(inherently_safe=inherently_safe)(send_message)
