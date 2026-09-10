"""The Monty stepping logic: run a sandboxed script forward to each awaited batch of host calls.

This is the body of the two Code Mode activities, split out from :mod:`.activities` for ONE
reason: it is the only part that touches ``pydantic_monty`` (the optional ``code-mode`` extra),
so keeping it here lets it import Monty normally, at module scope, and be type-checked against
the real package like any other module — while :mod:`.activities` stays importable on a worker
that doesn't have the extra and can report its absence as an actionable activity failure.

**Import this module only after checking that the extra is installed** — importing it without
``pydantic_monty`` raises ImportError. :func:`.activities._require_code_mode_extra` is that
check, and the two activities call it before importing anything from here.

=== Async / concurrent driver (FutureSnapshot batches) ===

Monty's *synchronous* stepping API (``Monty(code).start()`` / ``snapshot.resume(...)``) runs the
sandboxed script until it calls a function the sandbox doesn't define — at which point it
SUSPENDS and hands the host the pending call. The host does the real work, then ``resume(...)``
continues from exactly that point.

Host functions are ``async`` (the stubs declare ``async def``; scripts ``await`` them), so a
script runs host calls CONCURRENTLY with ``await asyncio.gather(search_flights(...),
search_hotels(...))``. Monty's async-host-function protocol over the synchronous stepping API
makes that work:

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
workflow. A purely sequential script (``await`` one call at a time) just produces single-call
batches; a synchronous (non-awaited) script is not supported — host functions are async by
contract.

No ``from __future__ import annotations`` — matching :mod:`.batch_models` and the rest of the
agent's Temporal-facing modules, whose annotations must stay runtime-resolvable.
"""

from typing import Any

import pydantic_monty as monty
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .batch_models import (
    CodeBatchStep,
    PendingCall,
    ResumeBatchInput,
)


def _drive_to_batch(snap: Any, stdout: monty.CollectString) -> CodeBatchStep:
    """Run Monty forward, deferring each external call as a future, until it awaits a batch.

    Loops: a FunctionSnapshot (an ``async`` host call) is recorded and resumed with
    ``{"future": ...}`` so execution continues; a FutureSnapshot means the script is now
    awaiting — return its ``pending_call_ids`` (resolved to the recorded calls) for the
    workflow to run; MontyComplete means done. Any ``snapshot.resume`` may raise a
    MontyError if the script errors mid-run — the callers translate that to ``error``."""
    seen: dict[int, PendingCall] = {}
    while True:
        if isinstance(snap, monty.MontyComplete):
            return CodeBatchStep(
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
            return CodeBatchStep(
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


def start_batch(script: str, type_check_stubs: str | None = None) -> CodeBatchStep:
    """Compile + start ``script`` (async driver), running to the first awaited batch or done.

    Type-checks against ``type_check_stubs`` when provided (Code Mode always passes the
    auto-generated host-function stubs). See this module's async-driver section for the
    defer-future / FutureSnapshot protocol. The body of
    :func:`.activities.code_start_batch`."""
    activity.logger.info(
        "code_start_batch: compiling + starting (script_len=%d, type_check=%s)",
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
        activity.logger.warning("code_start_batch: %s: %s", type(e).__name__, e)
        return CodeBatchStep(
            done=True, stdout=stdout.output, error=f"{type(e).__name__}: {e}"
        )
    activity.logger.info(
        "code_start_batch: %s",
        "done"
        if step.done
        else f"awaiting {[c.function_name for c in step.pending]}",
    )
    return step


def resume_batch(input: ResumeBatchInput) -> CodeBatchStep:
    """Resume a FutureSnapshot with the workflow-computed results, then run to the next batch.

    ``input.results`` carries one :class:`CallResult` per call the workflow ran (the batch
    that was awaited). Resumes the FutureSnapshot with ``{call_id: {"return_value": ...}}``
    for all of them, then drives forward to the next awaited batch or completion. The body of
    :func:`.activities.code_resume_batch`."""
    stdout = monty.CollectString()
    snap = monty.load_snapshot(input.snapshot, print_callback=stdout)
    if not isinstance(snap, monty.FutureSnapshot):
        raise ApplicationError(
            f"code_resume_batch expected a FutureSnapshot, got {type(snap).__name__}",
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
        activity.logger.warning("code_resume_batch: %s: %s", type(e).__name__, e)
        return CodeBatchStep(
            done=True, stdout=stdout.output, error=f"{type(e).__name__}: {e}"
        )
    activity.logger.info(
        "code_resume_batch: %s",
        "done"
        if step.done
        else f"awaiting {[c.function_name for c in step.pending]}",
    )
    return step
