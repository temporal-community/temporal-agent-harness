"""The Monty stepping engine: run a sandboxed script forward to each batch of host calls it awaits.

This is the only module that imports ``pydantic_monty`` (the optional ``code-mode`` extra). It
runs in WORKFLOW code, imported through the workflow sandbox's pass-through, so importing the
rest of the package (and ``harness.agent``) never needs the extra.

=== Why the sandbox steps inside the workflow ===

Monty 1.0 executes every script in a pooled ``monty`` subprocess, and its synchronous API
(``MontySession.feed_start`` / ``snapshot.resume``) blocks the calling thread, with the GIL
released, until the script suspends at a host call or finishes. No event loop and no threads
are involved, so the workflow can step the sandbox directly. Durability comes from replay: the
script is deterministic given its host calls' results, which history records, so re-running it
on replay reaches the same suspensions. Only the host calls are activities.

Determinism holds because nothing the script observes comes from the worker. Monty hashes and
orders sets the same way in every process, and the session's clock, entropy and sleeps are
routed to the host (``OS_POLICY``), where :class:`~.driver.CodeModeDriver` answers them from
``workflow.now()``, a seeded generator and durable timers.

=== Async / concurrent driver (FutureSnapshot batches) ===

Host functions are ``async`` (the stubs declare ``async def``; scripts ``await`` them), so a
script runs host calls concurrently with ``await asyncio.gather(search_flights(...),
search_hotels(...))``:

  * When the script calls an ``async def`` host function, Monty yields a FunctionSnapshot.
    Instead of resolving it, the step resumes with ``{"future": ...}`` ("this call is pending,
    keep running") and records the call by its id.
  * When the script ``await``\\ s, Monty yields a FutureSnapshot whose ``pending_call_ids`` are
    the calls it is blocked on. The step ends there, and the driver runs those calls as durable
    activities, concurrently, before resuming with their results.

Calls are recorded for the whole run, so a call made in one step and awaited in a later one
resolves like any other.

=== Holding no worker while the workflow waits ===

Each step checks a worker out of a process-wide pool only while it runs. At the suspension the
step dumps the session (a few KB, kept in workflow memory, never in history) and returns the
worker; the next step restores the dump into a fresh checkout. An agent awaiting a slow tool or
a human approval therefore pins no subprocess, and a workflow evicted from the cache leaks none.

=== Bounding a step ===

A script that loops forever would block the workflow thread for good: Temporal's deadlock
detector reports it but cannot interrupt native code. Each step therefore takes a
``time_limit``, enforced by the pool's ``request_timeout`` watchdog, which kills a worker that
runs longer than that between host interactions; the step then comes back ``aborted``. The
limit is a property of the pool, not of the session, so it never travels inside a dump: a step
replayed under a different limit than it first ran under behaves the same. Whether a step
overran depends on the machine, so the driver records each abort durably and replays it
without re-running the step (see :mod:`.driver`). Every sandbox limit Monty enforces itself
(recursion depth, suspension count) counts deterministically and needs no special handling.

=== Calling a stub type ===

The ``TypedDict``\\ s in the stubs exist only for the type checker, so the sandbox does not
define them. A script that calls one as a value (``StepSpec(name=..., prompt=...)``) passes the
type check, and at run time the sandbox surfaces the call like a host-function call. The step
answers it with the ``dict`` the arguments build, which is what calling a ``TypedDict`` returns
in CPython. The driver never sees these calls.
"""

import atexit
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pydantic_monty as monty

# `Monty()` imports this lazily to find the `monty` binary. Imported here, while this module
# loads outside the workflow sandbox, so that lazy import never runs under the sandbox.
import pydantic_monty._binary  # noqa: F401

from .stubs import TypeCheckStubs

# Route everything that would make a script depend on the worker to the host.
OS_POLICY: monty.OSPolicy = {
    "datetime": "call_host",
    "sleep": "call_host",
    "random_start": "call_host",
}

# How long type_check lets a clean script run before discarding it, and how many times it tries
# a check whose worker crashed (see type_check).
_TYPE_CHECK_RUN_LIMIT_SECS = 0.05
_TYPE_CHECK_ATTEMPTS = 3

_pools_lock = threading.Lock()
_pools: dict[float | None, monty.Monty] = {}


def _pool(time_limit: float | None) -> monty.Monty:
    """The process-wide worker pool whose watchdog kills a turn after ``time_limit`` seconds
    (``None``: no watchdog), spawned on first use and shared by every workflow."""
    with _pools_lock:
        pool = _pools.get(time_limit)
        if pool is None:
            pool = monty.Monty(request_timeout=time_limit)
            pool.__enter__()
            atexit.register(pool.__exit__, None, None, None)
            _pools[time_limit] = pool
        return pool


@dataclass(frozen=True)
class HostCall:
    """One async host call the script made, keyed by Monty's call id."""

    call_id: int
    function_name: str
    args: list[Any]
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class Sleep:
    """An ``await asyncio.sleep(seconds)`` the script made, answered with a durable timer."""

    call_id: int
    seconds: float


@dataclass(frozen=True)
class Step:
    """Where a step left the script: awaiting ``awaiting``, or finished.

    A finished script has ``output`` (its final value), or ``error`` when it failed: a syntax or
    type error, an uncaught exception, or one of Monty's own limits. ``aborted`` marks a step
    the sandbox lost in a way that may not recur on replay, with ``timed_out`` telling the
    watchdog apart from any other crash of the worker."""

    awaiting: list[HostCall | Sleep] = field(default_factory=list)
    done: bool = False
    output: Any = None
    error: str | None = None
    aborted: bool = False
    timed_out: bool = False


# Answers one of the session's clock / entropy / environment calls: (name, args) -> value.
# Raises NotImplementedError to leave a call unhandled.
OsAnswer = Callable[[str, tuple[Any, ...]], Any]


class ScriptRun:
    """One script's run, stepped from batch to batch.

    ``answer_os`` answers the session's clock, entropy and environment calls (see
    ``OS_POLICY``) and must be deterministic under replay. Not thread-safe; one run per
    script."""

    def __init__(self, script: str, stubs: TypeCheckStubs, *, answer_os: OsAnswer) -> None:
        self._script = script
        self._stubs = stubs
        self._answer_os = answer_os
        self._calls: dict[int, HostCall | Sleep] = {}
        self._dump: bytes | None = None
        self.stdout = monty.CollectString()

    def start(self, *, time_limit: float | None) -> Step:
        """Type-check and start the script, running it to its first awaited batch or done."""

        def begin(session: monty.MontySession) -> Any:
            return session.feed_start(self._script, print_callback=self.stdout)

        return self._step(begin, time_limit)

    def resume(self, results: Mapping[int, Any], *, time_limit: float | None) -> Step:
        """Resume the awaited batch with ``results`` (call id -> return value, or the exception
        the call failed with, which the script sees raised at its ``await``), then run on to the
        next batch or done. Safe to repeat after an aborted attempt: it restores the same dump.

        Monty raises a built-in exception type as itself and any other as ``Exception``, with
        the same message."""
        settled: dict[int, monty.ExternalSettledResult] = {
            call_id: {"exception": value} if isinstance(value, Exception) else {"return_value": value}
            for call_id, value in results.items()
        }

        def restore(session: monty.MontySession) -> Any:
            if self._dump is None:
                raise RuntimeError("ScriptRun.resume called before the script awaited a batch")
            snap = session.load_snapshot(self._dump, print_callback=self.stdout)
            if not isinstance(snap, monty.FutureSnapshot):
                raise RuntimeError(
                    f"expected a suspended FutureSnapshot, restored {type(snap).__name__}"
                )
            return snap.resume(settled)

        return self._step(restore, time_limit)

    def _step(self, begin: Callable[[monty.MontySession], Any], time_limit: float | None) -> Step:
        checkout = _pool(time_limit).checkout(
            type_check=True, type_check_stubs=self._stubs.source, os_policy=OS_POLICY
        )
        try:
            with checkout as session:
                return self._drive(begin(session))
        except monty.MontyCrashedError as e:
            return Step(done=True, aborted=True, timed_out=e.timed_out, error=str(e))
        except monty.MontyError as e:
            return Step(done=True, error=f"{type(e).__name__}: {e}")

    def _drive(self, snap: Any) -> Step:
        """Run the session forward, deferring each host call as a future, until it awaits."""
        while True:
            if isinstance(snap, monty.MontyComplete):
                return Step(done=True, output=snap.output)
            if isinstance(snap, monty.FunctionSnapshot):
                if snap.is_os_function:
                    snap = self._os_call(snap)
                elif snap.function_name in self._stubs.type_names:
                    try:
                        value = dict(*snap.args, **snap.kwargs)
                    except (TypeError, ValueError) as e:
                        snap = snap.resume({"exception": e})
                    else:
                        snap = snap.resume({"return_value": value})
                else:
                    self._calls[snap.call_id] = HostCall(
                        call_id=snap.call_id,
                        function_name=str(snap.function_name),
                        args=list(snap.args),
                        kwargs=dict(snap.kwargs),
                    )
                    snap = snap.resume({"future": ...})
                continue
            if isinstance(snap, monty.FutureSnapshot):
                self._dump = snap.dump()
                # Monty lists the pending ids in an order that differs from process to process;
                # ids are assigned in call order, so sorting makes the batch replay the same.
                pending = sorted(snap.pending_call_ids)
                return Step(awaiting=[self._calls.pop(i) for i in pending])
            # NameLookupSnapshot: leave the name undefined so the script raises NameError.
            snap = snap.resume()

    def _os_call(self, snap: monty.FunctionSnapshot) -> Any:
        """Answer one of the session's OS calls, or leave it unhandled."""
        name = snap.function_name
        if name == "asyncio.sleep":
            self._calls[snap.call_id] = Sleep(call_id=snap.call_id, seconds=float(snap.args[0]))
            return snap.resume({"future": ...})
        if name == "time.sleep":
            return snap.resume(
                {"exception": RuntimeError("time.sleep is unavailable; use `await asyncio.sleep(...)`")}
            )
        try:
            value = self._answer_os(name, tuple(snap.args))
        except NotImplementedError:
            # Filesystem and the like: Monty's default error for an unavailable OS call.
            return snap.resume_not_handled()
        return snap.resume({"return_value": value})


def type_check(script: str, stubs: TypeCheckStubs) -> str | None:
    """Check ``script`` against ``stubs`` exactly as :meth:`ScriptRun.start` does, without
    running it.

    Returns the checker's syntax or type errors, each with its line, or ``None`` when the script
    is clean. Monty has no check-only entry point, but ``feed_start`` checks the snippet before
    executing any of it, so a clean script is started and then discarded: it runs only until its
    first suspension (or a brief time limit), no host call it makes is ever answered, and how far
    it got is never looked at, so the report depends on the checker alone.

    A worker that crashes says nothing about the script, so the check is retried a few times
    and then raises, rather than reporting an answer a replay might not reproduce."""
    for _ in range(_TYPE_CHECK_ATTEMPTS - 1):
        try:
            return _type_check_once(script, stubs)
        except monty.MontyCrashedError:
            continue
    return _type_check_once(script, stubs)


def _type_check_once(script: str, stubs: TypeCheckStubs) -> str | None:
    checkout = _pool(None).checkout(
        limits={"max_turn_duration_secs": _TYPE_CHECK_RUN_LIMIT_SECS},
        type_check=True,
        type_check_stubs=stubs.source,
        os_policy=OS_POLICY,
    )
    try:
        with checkout as session:
            session.feed_start(script, print_callback=monty.CollectString())
    except (monty.MontySyntaxError, monty.MontyTypingError) as e:
        return f"{type(e).__name__}: {e}"
    except monty.MontyCrashedError:
        raise
    except monty.MontyError:
        pass  # the check passed; what the script then did is irrelevant
    return None
