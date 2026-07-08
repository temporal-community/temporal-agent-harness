"""Worker-side activities that step the Monty sandbox, surfacing host calls for the workflow.

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
    workflow. So the workflow stays the pure orchestrator: it calls ``code_start_batch``
    once, then alternates running each awaited batch of host calls (durable activities,
    concurrently) with ``code_resume_batch`` until the script completes.

This module imports ``pydantic_monty`` (the optional ``code-mode`` extra). It is WORKER-SIDE
only — never import it from workflow code or from ``harness.agent``. The workflow-side driver
dispatches these activities by NAME (``CODE_START_BATCH_ACTIVITY`` /
``CODE_RESUME_BATCH_ACTIVITY`` in :mod:`.batch_models`), so it never imports this module and
thus never requires ``pydantic_monty`` to be installed.

The snapshot crosses the activity boundary as bytes (``FutureSnapshot.dump()`` /
``load_snapshot``). Those bytes can be large and end up in workflow history, which is why
every client/worker must connect with the large-payload offload data converter.

No ``from __future__ import annotations`` — the models crossing Temporal's pydantic converter
require concrete annotations (see :mod:`.batch_models`).

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
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .batch_models import (
    CODE_RESUME_BATCH_ACTIVITY,
    CODE_START_BATCH_ACTIVITY,
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


@activity.defn(name=CODE_START_BATCH_ACTIVITY)
async def code_start_batch(
    script: str, type_check_stubs: str | None = None
) -> CodeBatchStep:
    """Compile + start ``script`` (async driver), running to the first awaited batch or done.

    Type-checks against ``type_check_stubs`` when provided (Code Mode always passes the
    auto-generated host-function stubs). See the module's async-driver section for the
    defer-future / FutureSnapshot protocol."""
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


@activity.defn(name=CODE_RESUME_BATCH_ACTIVITY)
async def code_resume_batch(input: ResumeBatchInput) -> CodeBatchStep:
    """Resume a FutureSnapshot with the workflow-computed results, then run to the next batch.

    ``input.results`` carries one :class:`CallResult` per call the workflow ran (the batch
    that was awaited). Resumes the FutureSnapshot with ``{call_id: {"return_value": ...}}``
    for all of them, then drives forward to the next awaited batch or completion."""
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


# The two sandbox-stepping activities every Code Mode worker registers, regardless of which
# tools its Code Mode tools expose (they are tool-agnostic — the tools are dispatched through the
# runner as their own activities). Register the durable bodies of any activity-backed host tools
# separately, the same way you register any @agent.activity_tool_defn (via agent.tool_activity)::
#
#     Worker(..., activities=[*CODE_MODE_ACTIVITIES, agent.tool_activity(my_tool), ...])
CODE_MODE_ACTIVITIES = [code_start_batch, code_resume_batch]
