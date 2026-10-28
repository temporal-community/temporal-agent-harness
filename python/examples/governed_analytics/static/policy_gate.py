"""``PolicyGate``: ask the tool policy before every activity, and tell it what each one returned.

``await gate.execute(activity, *args)`` asks the Dogwood verifier's ``VerifyToolCalls``
operation, over Nexus from the workflow, whether the call may run, the same question the
agent's evaluator asks: the action ``<prefix>/<activity name>``, with the call's arguments (or
what a :data:`PolicyInputBuilder` makes of them) as ``arg0``, plus any checks the builder adds.

  * **Approved**: the activity runs, and then its result is recorded with the verifier as
    ``<prefix>/<activity name>/result``, with the result as ``arg0`` and the call's arguments
    as ``arg1``, as the agent's harness records its tools' results. A policy can then judge a
    call by what the run did before it. ``execute`` returns only once the result is recorded,
    retrying while the verifier is unreachable, so no later call is judged without it.
  * **Refused**: :class:`PolicyRefused` is raised, and the workflow fails with the policy's
    reason. The activity never runs.
  * **Undecided** (the verifier is unreachable, or answers retry-later): the workflow sleeps
    and asks again. Nothing runs without an approval.

The questions, and the results recorded with the verifier, are Nexus operations in the
workflow's history. A gate holds only its settings, nothing about any run, so one can be
shared at module scope by every run of a workflow.

No ``from __future__ import annotations`` here (stringized annotations trip the pydantic
converter).
"""

import inspect
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel
from pydantic_core import to_jsonable_python
from temporalio import workflow
from temporalio.common import RawValue
from temporalio.exceptions import ApplicationError, NexusOperationError

SERVICE = "tool-policy"
VERIFY_OPERATION = "VerifyToolCalls"
RECORD_OPERATION = "RecordInputEvents"
OPERATION_TIMEOUT = timedelta(seconds=10)
# How long to wait before asking again when the verifier can't decide.
RETRY_DELAY = timedelta(seconds=10)

_APPROVED = "DECISION_APPROVED"
_RETRY_LATER = "DECISION_RETRY_LATER"
_VALUE_TYPE_URL = "type.googleapis.com/google.protobuf.Value"

ActivityResult = TypeVar("ActivityResult")


class PolicyCheck(BaseModel):
    """One more action asked about with a call, such as ``read_table`` for each table a query
    reads. The verifier decides a request's actions together."""

    tool: str
    arg0: dict[str, JsonValue]


class PolicyInput(BaseModel):
    """What the policy sees of one call: its ``arg0``, and the checks sent with it."""

    arg0: dict[str, JsonValue]
    checks: list[PolicyCheck] = Field(default_factory=list)


PolicyInputBuilder = Callable[[str, dict[str, Any]], Awaitable[PolicyInput]]
"""Builds what the policy sees of one activity's calls, from the activity's name and its
arguments by parameter name. It runs in the workflow, so it may run an activity of its own."""


class PolicyRefused(ApplicationError):
    """A call the tool policy refused. Uncaught, it fails the workflow."""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(
            f"The tool policy refused {action}: {reason}", type="PolicyRefused", non_retryable=True
        )
        self.action = action
        self.reason = reason


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class _Caller(_Wire):
    namespace: str
    subject: str
    task_queue: str
    workflow_id: str
    run_id: str


class _ToolCall(_Wire):
    name: str
    parameters: list[dict[str, JsonValue]]


class _VerifyRequest(_Wire):
    caller: _Caller
    tool_calls: list[_ToolCall]


class _VerifyResponse(_Wire):
    # The verifier answers in protobuf's JSON mapping, which leaves out default values.
    decision: str = "DECISION_UNSPECIFIED"
    reason: str = ""


class _InputEvent(_Wire):
    kind: str
    name: str
    payloads: list[dict[str, JsonValue]]
    update_id: str


class _RecordRequest(_Wire):
    caller: _Caller
    events: list[_InputEvent]


class PolicyGate:
    """Runs a workflow's activities only once the tool policy allows them. It holds only its
    settings, so one gate serves every run."""

    def __init__(
        self,
        *,
        endpoint: str,
        action_prefix: str,
        builders: Mapping[str, PolicyInputBuilder] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._prefix = action_prefix
        self._builders = dict(builders or {})

    async def execute(
        self,
        activity: Callable[..., Awaitable[ActivityResult]],
        *args: Any,
        summary: str,
        start_to_close_timeout: timedelta = timedelta(seconds=30),
    ) -> ActivityResult:
        """Run ``activity`` with ``args`` if the policy allows it; raise
        :class:`PolicyRefused` if it doesn't."""
        name = _activity_name(activity)
        bound = inspect.signature(activity).bind(*args)
        bound.apply_defaults()
        builder = self._builders.get(name)
        if builder is None:
            built = PolicyInput(arg0=to_jsonable_python(dict(bound.arguments)))
        else:
            built = await builder(name, dict(bound.arguments))
        await self._until_decided(f"{self._prefix}/{name}", built)
        result = await workflow.execute_activity(
            activity,
            args=args,
            start_to_close_timeout=start_to_close_timeout,
            summary=summary,
        )
        await self._record(
            _InputEvent(
                kind="INPUT_EVENT_KIND_ACTIVITY_RESULT",
                name=f"{self._prefix}/{name}/result",
                payloads=[
                    _value(to_jsonable_python(result)),
                    _value(to_jsonable_python(dict(bound.arguments))),
                ],
                # The verifier deduplicates events by this ID.
                update_id=f"activity-result/{workflow.uuid4()}",
            )
        )
        return result

    async def _until_decided(self, action: str, built: PolicyInput) -> None:
        while True:
            response = await self._verify(action, built)
            if response is None or response.decision == _RETRY_LATER:
                await workflow.sleep(
                    RETRY_DELAY, summary=f"tool policy undecided on {action}; asking again"
                )
            elif response.decision == _APPROVED:
                return
            else:
                raise PolicyRefused(action, response.reason)

    async def _verify(self, action: str, built: PolicyInput) -> _VerifyResponse | None:
        """The policy's answer, or ``None`` when the verifier couldn't be asked."""
        calls = [
            _ToolCall(name=action, parameters=[_value(built.arg0)]),
            *(
                _ToolCall(name=f"{self._prefix}/{check.tool}", parameters=[_value(check.arg0)])
                for check in built.checks
            ),
        ]
        request = _VerifyRequest(caller=_caller(), tool_calls=calls)
        try:
            raw = await self._client().execute_operation(
                VERIFY_OPERATION,
                request.model_dump(mode="json", by_alias=True),
                # The verifier answers with a json/protobuf payload; read its JSON directly.
                output_type=RawValue,
                schedule_to_close_timeout=OPERATION_TIMEOUT,
                summary=f"verify {action}",
            )
        except NexusOperationError:
            workflow.logger.warning("the tool policy could not be asked about %s", action)
            return None
        return _VerifyResponse.model_validate_json(raw.payload.data)

    async def _record(self, event: _InputEvent) -> None:
        """Tell the verifier what the run received, retrying until it has: a later call judged
        without it could be allowed when it shouldn't."""
        request = _RecordRequest(caller=_caller(), events=[event])
        while True:
            try:
                await self._client().execute_operation(
                    RECORD_OPERATION,
                    request.model_dump(mode="json", by_alias=True),
                    output_type=RawValue,
                    schedule_to_close_timeout=OPERATION_TIMEOUT,
                    summary=f"record {event.name}",
                )
                return
            except NexusOperationError:
                workflow.logger.warning("the tool policy could not be told of %s", event.name)
                await workflow.sleep(RETRY_DELAY, summary=f"record {event.name}: retrying")

    def _client(self) -> workflow.NexusClient[Any]:
        return workflow.create_nexus_client(service=SERVICE, endpoint=self._endpoint)


def _activity_name(activity: Callable[..., Any]) -> str:
    definition = getattr(activity, "__temporal_activity_definition", None)
    if definition is None or not definition.name:
        raise TypeError(f"{activity!r} is not an @activity.defn activity")
    return definition.name


def _caller() -> _Caller:
    info = workflow.info()
    return _Caller(
        namespace=info.namespace,
        subject=info.workflow_type,
        task_queue=info.task_queue,
        workflow_id=info.workflow_id,
        run_id=info.run_id,
    )


def _value(value: JsonValue) -> dict[str, JsonValue]:
    return {"@type": _VALUE_TYPE_URL, "value": value}
