"""Activities that step the Monty sandbox, surfacing host calls for the workflow to run.

The core of the suspend-at-external-call design. Monty's *synchronous* stepping API
(``Monty(code).start()`` / ``snapshot.resume(...)``) runs the sandboxed script until it
calls a function the sandbox doesn't define — at which point it SUSPENDS and hands the host
the pending call. The host does the real work, then ``resume(...)`` continues from exactly
that point. Host functions are ``async`` (scripts ``await`` them), so concurrency is
expressed naturally with ``asyncio.gather`` — see the driver section below.

Why this lives in activities (not the workflow):
  * Monty's *async* API (``acreate``/``run_async``) cannot run in a Temporal workflow at
    all — its Rust↔asyncio bridge needs ``loop.call_soon_threadsafe``, which the
    deterministic workflow loop forbids (it raises NotImplementedError). The synchronous
    ``start``/``resume`` API sidesteps that entirely — no event loop, no threads. (The
    async *concurrency* the script sees does NOT use Monty's async API; it's the
    defer-future / FutureSnapshot protocol over this synchronous stepping API.)
  * Even so, executing arbitrary (sandboxed) user code is not something to do inside a
    workflow. So the workflow stays the pure orchestrator: it calls ``monty_start_batch``
    once, then alternates running each awaited batch of host calls (durable activities,
    concurrently) with ``monty_resume_batch`` until the script completes.

The snapshot crosses the activity boundary as bytes (``FutureSnapshot.dump()`` /
``load_snapshot``). Those bytes can be large and end up in workflow history, which is why
the worker connects with the large-payload offload data converter.

No ``from __future__ import annotations`` — the models below cross Temporal's pydantic
converter, and stringized annotations trip its TypeAdapter build.

=== Async / concurrent driver (FutureSnapshot batches) ===

Host functions are ``async`` (the stubs declare ``async def``; scripts ``await`` them), so
a script runs host calls CONCURRENTLY with ``await asyncio.gather(search_flights(...),
search_hotels(...))``. Monty's async-host-function protocol over the synchronous stepping
API makes that work:

  * When the script calls an ``async def`` host function, Monty yields a FunctionSnapshot.
    Instead of resolving it, we resume with ``{"future": ...}`` (an ExternalFuture) — "this
    call is pending, keep running" — and record its name/args by ``call_id``.
  * The script keeps issuing host calls (each deferred) until it ``await``s them. At that
    point Monty yields a FutureSnapshot whose ``pending_call_ids`` is the batch the script
    is blocked on. We hand that whole batch to the workflow, which runs every call
    CONCURRENTLY as its own durable activity (``asyncio.gather`` over ``execute_activity``)
    and resumes the FutureSnapshot with ``{call_id: {"return_value": ...}}`` for all of them.

So concurrency is real and durable: one Temporal activity per host call, all in flight at
once, orchestrated by the workflow. The defer loop (resuming ExternalFutures) does no host
work, so it runs entirely inside the activity — only the awaited batch crosses to the
workflow. A purely sequential script (``await`` one call at a time) just produces
single-call batches; a synchronous (non-awaited) script is not supported — host functions
are async by contract.
"""

from typing import Any

import pydantic_monty as monty
from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity
from temporalio.exceptions import ApplicationError

# Snapshot bytes are arbitrary binary (a serialized Monty continuation), so they are NOT
# valid UTF-8. Pydantic's default JSON encoding for ``bytes`` is UTF-8 and would fail
# ("invalid utf-8 sequence") — base64 encodes/decodes them losslessly on both legs.
_BYTES_AS_BASE64 = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")


class PendingCall(BaseModel):
    """One host call the script is awaiting, surfaced to the workflow to run durably.

    ``call_id`` keys the result back when resuming the FutureSnapshot. ``function_name`` /
    ``args`` / ``kwargs`` are the call the workflow dispatches (same shape the sequential
    ``MontyStep`` carries)."""

    call_id: int
    function_name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class MontyBatchStep(BaseModel):
    """One step of the async driver: either done, or blocked awaiting a batch of host calls.

    When ``done`` is False the script is awaiting ``pending`` (one or more concurrent host
    calls) and ``snapshot`` is the serialized FutureSnapshot to resume once their results are
    known. When ``done`` is True, ``output_json`` holds the script's final value or ``error``
    holds a script-level Monty error (a bad script is data, not a workflow failure)."""

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


class MontyResumeBatchInput(BaseModel):
    """Input to :func:`monty_resume_batch`: the serialized FutureSnapshot plus the results
    for every call the workflow just ran (one :class:`CallResult` per pending call)."""

    model_config = _BYTES_AS_BASE64

    snapshot: bytes
    results: list[CallResult] = Field(default_factory=list)


def _drive_to_batch(snap: Any, stdout: monty.CollectString) -> MontyBatchStep:
    """Run Monty forward, deferring each external call as a future, until it awaits a batch.

    Loops: a FunctionSnapshot (an ``async`` host call) is recorded and resumed with
    ``{"future": ...}`` so execution continues; a FutureSnapshot means the script is now
    awaiting — return its ``pending_call_ids`` (resolved to the recorded calls) for the
    workflow to run; MontyComplete means done. Any ``snapshot.resume`` may raise a
    MontyError if the script errors mid-run — the callers translate that to ``error``."""
    seen: dict[int, PendingCall] = {}
    while True:
        if isinstance(snap, monty.MontyComplete):
            return MontyBatchStep(
                done=True, stdout=stdout.output, output_json=snap.output_json()
            )
        if isinstance(snap, monty.FunctionSnapshot):
            seen[snap.call_id] = PendingCall(
                call_id=snap.call_id,
                function_name=str(snap.function_name),
                args=list(snap.args),
                kwargs=dict(snap.kwargs),
            )
            # ExternalFuture: "pending, keep going" — does no host work, so stay in-activity.
            snap = snap.resume({"future": ...})
            continue
        if isinstance(snap, monty.FutureSnapshot):
            ids = list(snap.pending_call_ids)
            missing = [i for i in ids if i not in seen]
            if missing:
                # A call deferred in a PRIOR activity run and only awaited now: this driver
                # records calls per-run, so it can't name them. Model-generated gather code
                # issues+awaits together, so this shouldn't arise; fail loudly if it does.
                raise ApplicationError(
                    f"FutureSnapshot awaits call_ids {missing} not seen in this run "
                    f"(cross-batch deferral is unsupported)",
                    type="UnsupportedMontyFuture",
                    non_retryable=True,
                )
            return MontyBatchStep(
                done=False,
                stdout=stdout.output,
                snapshot=snap.dump(),
                pending=[seen[i] for i in ids],
            )
        # NameLookupSnapshot (unknown bare name) — unsupported by this driver.
        raise ApplicationError(
            f"unsupported Monty suspension: {type(snap).__name__}",
            type="UnsupportedMontySuspension",
            non_retryable=True,
        )


@activity.defn
async def monty_start_batch(
    script: str, type_check_stubs: str | None = None
) -> MontyBatchStep:
    """Compile + start ``script`` (async driver), running to the first awaited batch or done.

    Type-checks against ``type_check_stubs`` when provided (the conversational agent always
    passes async host-function stubs). See the module's async-driver section for the
    defer-future / FutureSnapshot protocol."""
    activity.logger.info(
        "monty_start_batch: compiling + starting (script_len=%d, type_check=%s)",
        len(script),
        type_check_stubs is not None,
    )
    stdout = monty.CollectString()
    try:
        instance = monty.Monty(
            script,
            type_check=type_check_stubs is not None,
            type_check_stubs=type_check_stubs,
        )
        snap = instance.start(print_callback=stdout)
        step = _drive_to_batch(snap, stdout)
    except monty.MontyError as e:
        activity.logger.warning("monty_start_batch: %s: %s", type(e).__name__, e)
        return MontyBatchStep(
            done=True, stdout=stdout.output, error=f"{type(e).__name__}: {e}"
        )
    activity.logger.info(
        "monty_start_batch: %s",
        "done"
        if step.done
        else f"awaiting {[c.function_name for c in step.pending]}",
    )
    return step


@activity.defn
async def monty_resume_batch(input: MontyResumeBatchInput) -> MontyBatchStep:
    """Resume a FutureSnapshot with the workflow-computed results, then run to the next batch.

    ``input.results`` carries one :class:`CallResult` per call the workflow ran (the batch
    that was awaited). Resumes the FutureSnapshot with ``{call_id: {"return_value": ...}}``
    for all of them, then drives forward to the next awaited batch or completion."""
    stdout = monty.CollectString()
    snap = monty.load_snapshot(input.snapshot, print_callback=stdout)
    if not isinstance(snap, monty.FutureSnapshot):
        raise ApplicationError(
            f"monty_resume_batch expected a FutureSnapshot, got {type(snap).__name__}",
            type="MontyResumeKind",
            non_retryable=True,
        )
    results_map: dict[int, Any] = {
        r.call_id: {"return_value": r.return_value} for r in input.results
    }
    try:
        resumed = snap.resume(results_map)
        step = _drive_to_batch(resumed, stdout)
    except monty.MontyError as e:
        activity.logger.warning("monty_resume_batch: %s: %s", type(e).__name__, e)
        return MontyBatchStep(
            done=True, stdout=stdout.output, error=f"{type(e).__name__}: {e}"
        )
    activity.logger.info(
        "monty_resume_batch: %s",
        "done"
        if step.done
        else f"awaiting {[c.function_name for c in step.pending]}",
    )
    return step
