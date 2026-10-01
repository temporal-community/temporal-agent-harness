"""The Monty stepping logic: run a sandboxed script forward to each awaited batch of host calls.

This is the body of the two Code Mode activities, split out from :mod:`.activities` for ONE
reason: it is the only part that touches ``pydantic_monty`` (the optional ``code-mode`` extra),
so keeping it here lets it import Monty normally, at module scope, and be type-checked against
the real package like any other module — while :mod:`.activities` stays importable on a worker
that doesn't have the extra and can report its absence as an actionable activity failure.

**Import this module only after checking that the extra is installed** — importing it without
``pydantic_monty`` raises ImportError. :func:`.activities._require_code_mode_extra` is that
check, and the two activities call it before importing anything from here.

=== Where the sandbox runs ===

Monty runs every script in a ``monty`` worker SUBPROCESS (the binary ships in the
``pydantic-monty-runtime`` dependency), owned by a pool. Each activity call opens its own
one-worker pool and closes it on return: a pool costs about ten milliseconds to start, and
holding none between calls keeps the activities stateless — nothing ties a call to the
worker process, the event loop, or an earlier call, so a retry or a different Temporal worker
picks up a script exactly where the snapshot left it.

=== Async / concurrent driver (FutureSnapshot batches) ===

``session.feed_start(script)`` runs the sandboxed script until it calls a function the sandbox
doesn't define — at which point it SUSPENDS and hands the host the pending call. The host does
the real work, then ``resume(...)`` continues from exactly that point.

Host functions are ``async`` (the stubs declare ``async def``; scripts ``await`` them), so a
script runs host calls CONCURRENTLY with ``await asyncio.gather(search_flights(...),
search_hotels(...))``. Monty's async-host-function protocol makes that work:

  * When the script calls an ``async def`` host function, Monty yields a FunctionSnapshot.
    Instead of resolving it, we resume with ``{"future": ...}`` (an ExternalFuture) — "this
    call is pending, keep running" — and record its name/args by ``call_id``.
  * The script keeps issuing host calls (each deferred) until it ``await``s them. At that
    point Monty yields a FutureSnapshot whose ``pending_call_ids`` is the batch the script
    is blocked on. We hand that whole batch to the workflow, which runs every call
    CONCURRENTLY as its own durable activity (``asyncio.gather`` over ``execute_activity``)
    and resumes the FutureSnapshot with ``{call_id: {"return_value": ...}}`` for all of them
    — or ``{"exception": PermissionError(...)}`` for a call the approval policy refused.

So concurrency is real and durable: one Temporal activity per host call, all in flight at
once, orchestrated by the workflow. The defer loop (resuming ExternalFutures) does no host
work, so it runs entirely inside the activity — only the awaited batch crosses to the
workflow, as the FutureSnapshot's dump, which the next activity restores into a fresh session.
A purely sequential script (``await`` one call at a time) just produces single-call batches; a
synchronous (non-awaited) script is not supported — host functions are async by contract.

=== What the host answers itself ===

The workflow sees only host-function calls. Three other suspensions are answered here:

  * a call to one of the stubs' ``TypedDict``\\ s, which exist only for the type checker, so the
    sandbox does not define them. A script that calls one as a value (``StepSpec(name=...,
    prompt=...)``) passes the type check, and at run time the sandbox surfaces the call like a
    host-function call. :func:`_drive_to_batch` answers it with the ``dict`` the arguments
    build, which is what calling a ``TypedDict`` returns in CPython;
  * an OS call (a file, the environment, a subprocess). Code Mode's sandbox has no filesystem,
    no network and no environment, so every OS call is answered "not handled" and fails inside
    the sandbox. Its only capabilities are the host functions;
  * a read of a name nothing defines, which is left undefined, so the sandbox raises
    ``NameError`` at it.

No ``from __future__ import annotations`` — matching :mod:`.batch_models` and the rest of the
agent's Temporal-facing modules, whose annotations must stay runtime-resolvable.
"""

import json
from collections.abc import Collection
from typing import Any

import pydantic_monty as monty
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .batch_models import (
    CodeBatchStep,
    PendingCall,
    ResumeBatchInput,
    TypeCheckStubs,
)

# How long a check-only feed may run before its first host call. The check itself happens
# before anything runs; this only bounds a script that computes for a while before calling out.
_TYPE_CHECK_LIMITS: monty.ResourceLimits = {"max_feed_duration_secs": 1.0}


def _pool() -> monty.AsyncMonty:
    """A one-worker pool for one activity call (see the module docstring)."""
    return monty.AsyncMonty(min_processes=1, max_processes=1)


def _jsonable(value: Any) -> Any:
    """The script's final value as JSON-native data, the shape ``CodeBatchStep`` carries it in.

    Sequences and sets become lists and mappings dicts, as ``json`` would have them. A value
    with no JSON shape — bytes, a sandbox class instance — is carried as its ``repr``, which is
    how the driver presents a final value to the model anyway."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return repr(value)


async def _drive_to_batch(
    snap: monty.AsyncSnapshot, stdout: monty.CollectString, type_names: Collection[str]
) -> CodeBatchStep:
    """Run Monty forward, deferring each external call as a future, until it awaits a batch.

    Loops: a FunctionSnapshot calling one of ``type_names`` (a stub ``TypedDict``) is answered
    with a dict, and an OS call or a bare undefined name is answered as in this module's
    docstring; any other FunctionSnapshot (an ``async`` host call) is recorded and resumed with
    ``{"future": ...}`` so execution continues; a FutureSnapshot means the script is now
    awaiting — return its ``pending_call_ids`` (resolved to the recorded calls) for the workflow
    to run; MontyComplete means done. Any ``resume`` may raise a MontyError if the script errors
    mid-run — the callers translate that to ``error``."""
    seen: dict[int, PendingCall] = {}
    while True:
        if isinstance(snap, monty.MontyComplete):
            return CodeBatchStep(
                done=True, stdout=stdout.output, output_json=json.dumps(_jsonable(snap.output))
            )
        if isinstance(snap, monty.AsyncFunctionSnapshot) and snap.is_os_function:
            snap = await snap.resume_not_handled()
            continue
        if isinstance(snap, monty.AsyncFunctionSnapshot) and snap.function_name in type_names:
            try:
                value = dict(*snap.args, **snap.kwargs)
            except (TypeError, ValueError) as e:
                snap = await snap.resume({"exception": e})
            else:
                snap = await snap.resume({"return_value": value})
            continue
        if isinstance(snap, monty.AsyncFunctionSnapshot):
            seen[snap.call_id] = PendingCall(
                call_id=snap.call_id,
                function_name=str(snap.function_name),
                args=list(snap.args),
                kwargs=dict(snap.kwargs),
            )
            # ExternalFuture: "pending, keep going" — does no host work, so stay in-activity.
            snap = await snap.resume({"future": ...})
            continue
        if isinstance(snap, monty.AsyncNameLookupSnapshot):
            snap = await snap.resume()
            continue
        if isinstance(snap, monty.AsyncFutureSnapshot):
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
        raise ApplicationError(
            f"unsupported Monty suspension: {type(snap).__name__}",
            type="UnsupportedMontySuspension",
            non_retryable=True,
        )


async def start_batch(script: str, stubs: TypeCheckStubs | None = None) -> CodeBatchStep:
    """Compile + start ``script`` (async driver), running to the first awaited batch or done.

    Type-checks against ``stubs`` when provided (Code Mode always passes the auto-generated
    host-function stubs), and answers the script's calls to their ``TypedDict``\\ s (see this
    module's docstring). See this module's async-driver section for the defer-future /
    FutureSnapshot protocol. The body of :func:`.activities.code_start_batch`."""
    activity.logger.info(
        "code_start_batch: compiling + starting (script_len=%d, type_check=%s)",
        len(script),
        stubs is not None,
    )
    stdout = monty.CollectString()
    async with _pool() as pool:
        async with pool.checkout(
            type_check=stubs is not None,
            type_check_stubs=stubs.source if stubs is not None else None,
        ) as session:
            try:
                snap = await session.feed_start(script, print_callback=stdout)
                step = await _drive_to_batch(
                    snap, stdout, stubs.type_names if stubs is not None else ()
                )
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


async def resume_batch(input: ResumeBatchInput) -> CodeBatchStep:
    """Resume a FutureSnapshot with the workflow-computed results, then run to the next batch.

    ``input.results`` carries one :class:`CallResult` per call the workflow ran (the batch
    that was awaited). Restores the snapshot into a fresh session (which brings back the
    script's type-check state with it) and resumes it with ``{call_id: {"return_value": ...}}``
    for all of them — or ``{"exception": PermissionError(...)}`` for a call the approval policy
    refused, so the script sees the refusal at that call's ``await`` while the rest of the batch
    delivers normally — then drives forward to the next awaited batch or completion. The body
    of :func:`.activities.code_resume_batch`."""
    stdout = monty.CollectString()
    results_map: dict[int, Any] = {
        r.call_id: (
            {"exception": PermissionError(r.permission_denied)}
            if r.permission_denied is not None
            else {"return_value": r.return_value}
        )
        for r in input.results
    }
    async with _pool() as pool:
        async with pool.checkout() as session:
            snap = await session.load_snapshot(input.snapshot, print_callback=stdout)
            if not isinstance(snap, monty.AsyncFutureSnapshot):
                raise ApplicationError(
                    f"code_resume_batch expected a FutureSnapshot, got {type(snap).__name__}",
                    type="MontyResumeKind",
                    non_retryable=True,
                )
            try:
                resumed = await snap.resume(results_map)
                step = await _drive_to_batch(resumed, stdout, input.type_names)
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


async def type_check(script: str, stubs: TypeCheckStubs) -> str | None:
    """Check ``script`` against ``stubs`` exactly as :func:`start_batch` does, without letting
    it act.

    Returns the checker's syntax or type errors, each with its line, or ``None`` when the
    script is clean. Monty checks a script as it is fed, before any of it runs; a clean script
    then starts, and this abandons it at its first suspension — no host call is answered, so it
    cannot reach anything, and a long computation before that point is cut off by
    :data:`_TYPE_CHECK_LIMITS`. A runtime error is not a type error, so it reports nothing. The
    body of :func:`.activities.code_type_check`."""
    async with _pool() as pool:
        async with pool.checkout(
            type_check=True, type_check_stubs=stubs.source, limits=_TYPE_CHECK_LIMITS
        ) as session:
            try:
                await session.feed_start(script, print_callback=monty.CollectString())
            except (monty.MontySyntaxError, monty.MontyTypingError) as e:
                return f"{type(e).__name__}: {e}"
            except monty.MontyRuntimeError:
                return None
    return None
