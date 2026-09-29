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

=== What the type checker misses ===

Monty's type checker and its runtime disagree in two ways, so a script can pass the check and
still fail once it runs — possibly after some host calls have already happened:

  * **Imports.** The checker resolves modules the sandbox does not have: the host stubs
    themselves, which it loads as a module named ``type_stubs``, and typeshed modules such as
    ``dataclasses`` or ``collections``. :func:`_import_errors` runs each of the script's import
    statements, alone, in a bare sandbox, so the runtime decides what is importable.
  * **Stub-only names.** The ``TypedDict``\\ s in the stubs exist only for the checker. In an
    annotation they are harmless, but used as a value (``AgentStep(name=...)``) the runtime
    suspends on them as an unknown external call or name. :func:`_stub_name_errors` reports
    each such use.

:func:`start_batch` and :func:`type_check` both run these after the type check, so every
script is checked the same way whether it is only being checked or about to run.

No ``from __future__ import annotations`` — matching :mod:`.batch_models` and the rest of the
agent's Temporal-facing modules, whose annotations must stay runtime-resolvable.
"""

import ast
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
    auto-generated host-function stubs), plus the checks the type checker misses (see this
    module's docstring), so a script that would fail on them never starts. See this module's
    async-driver section for the defer-future / FutureSnapshot protocol. The body of
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
        if type_check_stubs is not None:
            report = _runtime_report(script, type_check_stubs)
            if report:
                activity.logger.warning("code_start_batch: %s", report)
                return CodeBatchStep(done=True, error=report)
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


def type_check(script: str, type_check_stubs: str) -> str | None:
    """Check ``script`` against ``type_check_stubs`` exactly as :func:`start_batch` does, without
    running it.

    Returns every problem found — the type checker's syntax and type errors, then the imports and
    stub-only names that would fail at run time (see this module's docstring), each with its line
    — or ``None`` when the script is clean. Monty checks when the script is compiled, and nothing
    runs until ``start``, which this never calls on the script. The body of
    :func:`.activities.code_type_check`."""
    reports = []
    try:
        monty.Monty(script, type_check=True, type_check_stubs=type_check_stubs)
    except monty.MontyError as e:
        reports.append(f"{type(e).__name__}: {e}")
    runtime = _runtime_report(script, type_check_stubs)
    if runtime:
        reports.append(runtime)
    return "\n\n".join(reports) or None


def _runtime_report(script: str, type_check_stubs: str) -> str | None:
    """The problems in ``script`` the type checker passes but the runtime would not, as one
    report, or ``None``. A script that does not parse is left to the type checker to report."""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return None
    errors = sorted(_import_errors(tree) + _stub_name_errors(tree, type_check_stubs))
    if not errors:
        return None
    return "SandboxCheckError: " + "\n\n".join(
        f"error[{code}]: {message}\n --> main.py:{line}:{col}" for line, col, code, message in errors
    )


_Error = tuple[int, int, str, str]


def _import_errors(tree: ast.Module) -> list[_Error]:
    """Each import statement, anywhere in the script, that fails in a bare sandbox.

    Running the statement on its own is side-effect free (a sandbox import touches nothing
    outside it) and asks the runtime itself, so no list of available modules is kept here."""
    errors: list[_Error] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        statement = ast.unparse(node)
        try:
            monty.Monty(statement).start()
        except monty.MontyError as e:
            failure = str(e).strip().splitlines()[-1]
            message = f"`{statement}` fails when the script runs ({failure})."
            if (node.module if isinstance(node, ast.ImportFrom) else None) == "type_stubs" or any(
                alias.name == "type_stubs" for alias in getattr(node, "names", [])
            ):
                message += (
                    " `type_stubs` exists only for the type checker: the host functions and"
                    " their types are already in scope, so delete this import."
                )
            errors.append((node.lineno, node.col_offset + 1, "unavailable-import", message))
    return errors


def _stub_name_errors(tree: ast.Module, type_check_stubs: str) -> list[_Error]:
    """Each use, outside an annotation, of a name the stubs define only as a type.

    The stubs' classes (the ``TypedDict``\\ s of host-function arguments and results) are not
    defined at run time. A script that defines a name of its own is left alone."""
    stub_types = {
        node.name for node in ast.parse(type_check_stubs).body if isinstance(node, ast.ClassDef)
    }
    defined = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    } | {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    stub_types -= defined
    if not stub_types:
        return []

    in_annotations: set[int] = set()
    for node in ast.walk(tree):
        for annotation in (
            getattr(node, "annotation", None),
            getattr(node, "returns", None),
        ):
            if annotation is not None:
                in_annotations.update(id(n) for n in ast.walk(annotation))

    return [
        (
            node.lineno,
            node.col_offset + 1,
            "type-only-name",
            f"`{node.id}` is only a type for the checker and does not exist when the script "
            f"runs. Use it in annotations only; build the value as a plain dict literal.",
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in stub_types
        and id(node) not in in_annotations
    ]
