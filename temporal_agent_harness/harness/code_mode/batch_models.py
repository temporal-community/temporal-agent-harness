"""Workflow-safe pydantic models for the Code Mode async batch protocol.

These cross the Temporal activity boundary between the workflow-side driver
(:class:`~temporal_agent_harness.harness.code_mode.driver.CodeModeDriver`) and the worker-side
stepping activities (``code_start_batch`` / ``code_resume_batch`` in
:mod:`~temporal_agent_harness.harness.code_mode.activities`). They are deliberately sandbox-safe:
this module imports ONLY ``typing`` + ``pydantic`` — never ``pydantic_monty`` — so the workflow
can import it without dragging the sandbox engine into the deterministic loop.

The snapshot crosses the boundary as bytes (``FutureSnapshot.dump()`` / ``load_snapshot``).
Those bytes can be large and end up in workflow history, which is why every client/worker must
connect with the large-payload offload data converter
(:func:`temporal_agent_harness.utils.large_payload.with_large_payload_offload`).

No ``from __future__ import annotations`` — these cross Temporal's pydantic converter, and
stringized annotations trip its TypeAdapter build.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Activity names, kept here (sandbox-safe) so the workflow-side driver can dispatch by NAME
# (``workflow.execute_activity("code_start_batch", ...)``) WITHOUT importing the ``activities``
# module — which imports ``pydantic_monty`` (an optional extra). The worker-side activities
# register under exactly these names.
CODE_START_BATCH_ACTIVITY = "code_start_batch"
CODE_RESUME_BATCH_ACTIVITY = "code_resume_batch"

# Snapshot bytes are arbitrary binary (a serialized sandbox continuation), so they are NOT
# valid UTF-8. Pydantic's default JSON encoding for ``bytes`` is UTF-8 and would fail
# ("invalid utf-8 sequence") — base64 encodes/decodes them losslessly on both legs.
_BYTES_AS_BASE64 = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")


class PendingCall(BaseModel):
    """One host call the script is awaiting, surfaced to the workflow to run durably.

    ``call_id`` keys the result back when resuming the FutureSnapshot. ``function_name`` /
    ``args`` / ``kwargs`` are the call the workflow dispatches to the matching host tool."""

    call_id: int
    function_name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class CodeBatchStep(BaseModel):
    """One step of the async driver: either done, or blocked awaiting a batch of host calls.

    When ``done`` is False the script is awaiting ``pending`` (one or more concurrent host
    calls) and ``snapshot`` is the serialized FutureSnapshot to resume once their results are
    known. When ``done`` is True, ``output_json`` holds the script's final value or ``error``
    holds a script-level sandbox error (a bad script is data, not a workflow failure)."""

    model_config = _BYTES_AS_BASE64

    done: bool
    stdout: str = ""

    # Awaiting-a-batch fields (set when done is False).
    snapshot: bytes | None = None
    pending: list[PendingCall] = Field(default_factory=list)

    # Terminal fields (set when done is True).
    output_json: str | None = None
    error: str | None = None


class CallResult(BaseModel):
    """A host call's result, keyed by ``call_id`` for resuming the FutureSnapshot."""

    call_id: int
    return_value: Any = None


class ResumeBatchInput(BaseModel):
    """Input to ``code_resume_batch``: the serialized FutureSnapshot plus the results for
    every call the workflow just ran (one :class:`CallResult` per pending call)."""

    model_config = _BYTES_AS_BASE64

    snapshot: bytes
    results: list[CallResult] = Field(default_factory=list)
