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
resolves like any other. Each step also hands over the calls the script made since the last one,
so the driver can start them at once, even when the step ends somewhere they are not awaited.

=== File operations (FunctionSnapshot stops) ===

A file operation on a mounted path (see :mod:`.vfs`) is a synchronous call: the script cannot go
on without its value. The step ends right at that FunctionSnapshot, dumped like any other
suspension, and the driver carries the operation out before the next step resumes it with the
result. Operations on paths no mount covers stay unhandled, as without mounts.

=== Holding no worker while the workflow waits ===

Each step checks a worker out of a process-wide pool only while it runs. At the suspension the
step dumps the session (a few KB, kept in workflow memory, never in history) and returns the
worker; the next step restores the dump into a fresh checkout. An agent awaiting a slow tool or
a human approval therefore pins no subprocess, and a workflow evicted from the cache leaks none.

=== Bounding a step ===

A script that loops forever would block the workflow thread for good: Temporal's deadlock
detector reports it but cannot interrupt native code. Each step therefore runs in a pool whose
``request_timeout`` watchdog kills a worker that runs too long between host interactions; the
step then comes back ``aborted``. There are two pools, because the watchdog has opposite jobs
on either side of Temporal's two-second deadlock window. On a step's original run, the live
pool's watchdog fires first and decides the outcome: the script gets an error and the agent
carries on. On replay the outcome is already in history, so the replay pool's watchdog only
fires after the deadlock detector has failed the task, and merely frees the thread. The limit is
a property of the pool, not of the session, so it never travels inside a dump: a step resumed
from a dump made during replay still runs under the live limit. Whether a step overran depends
on the machine, so the driver records each abort durably and replays it without re-running the
step (see :mod:`.driver`). Every sandbox limit Monty enforces itself
(recursion depth, suspension count) counts deterministically and needs no special handling.

=== Calling a stub type ===

The ``TypedDict``\\ s in the stubs exist only for the type checker, so the sandbox does not
define them. A script that calls one as a value (``StepSpec(name=..., prompt=...)``) passes the
type check, and at run time the sandbox surfaces the call like a host-function call. The step
answers it with the ``dict`` the arguments build, which is what calling a ``TypedDict`` returns
in CPython. The driver never sees these calls.
"""

import atexit
import errno
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import pydantic_monty as monty

# `Monty()` imports this lazily to find the `monty` binary. Imported here, while this module
# loads outside the workflow sandbox, so that lazy import never runs under the sandbox.
import pydantic_monty._binary  # noqa: F401

from .stubs import TypeCheckStubs
from .vfs import FILE_OPERATIONS, FileOp, FileStat, Opened, VFSMount, is_write, route

# Route everything that would make a script depend on the worker to the host.
OS_POLICY: monty.OSPolicy = {
    "datetime": "call_host",
    "sleep": "call_host",
    "random_start": "call_host",
}

# How long type_check lets a clean script run before discarding it (see type_check).
_TYPE_CHECK_RUN_LIMIT_SECS = 0.05

# The longest a script may compute between host interactions (from its start to its first
# ``await``, or from one awaited batch to the next) before it is stopped with an error. The
# sandbox steps inside a workflow task, which Temporal's deadlock detector fails after two
# seconds, so this stays under that. Host calls run as activities with their own timeouts; this
# bounds only the script's own computation.
STEP_TIME_LIMIT_SECS = 1.0

# The limit a step runs under on replay, where it is known to have finished within
# ``STEP_TIME_LIMIT_SECS`` on its original run, so a slow replay is not cut short. Temporal's
# deadlock detector fails a replay that takes over two seconds, and nothing the step does after
# that is used; this only ends a step that never finishes, which frees its thread and lets the
# worker evict the workflow, since eviction waits for the deadlocked activation to return. It
# stays above two seconds so the deadlock detector, not this limit, is what fails a slow replay.
_REPLAY_BACKSTOP_SECS = 3.0

# What a script that a step's watchdog stopped, or whose worker crashed, ends with.
TIMED_OUT_ERROR = (
    f"TimeoutError: the script ran for more than {STEP_TIME_LIMIT_SECS:g}s without awaiting a "
    f"host function, and was stopped"
)
CRASHED_ERROR = "MontyCrashedError: the sandbox worker running the script crashed"


class _Pool:
    """A process-wide worker pool, spawned on first use and shared by every workflow."""

    def __init__(self, request_timeout: float) -> None:
        self._request_timeout = request_timeout
        self._lock = threading.Lock()
        self._pool: monty.Monty | None = None

    def __call__(self) -> monty.Monty:
        with self._lock:
            if self._pool is None:
                pool = monty.Monty(request_timeout=self._request_timeout)
                pool.__enter__()
                atexit.register(pool.__exit__, None, None, None)
                self._pool = pool
            return self._pool


_live_pool = _Pool(STEP_TIME_LIMIT_SECS)
_replay_pool = _Pool(_REPLAY_BACKSTOP_SECS)


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


# What a step can leave the script blocked on.
Awaitable = HostCall | Sleep | FileOp


@dataclass(frozen=True)
class Step:
    """Where a step left the script: awaiting ``awaiting``, or finished.

    ``awaiting`` is what the next resume needs a result for: the host calls of an awaited
    batch, or the one file operation the step stopped at. ``started`` are calls the script made
    during the step that it is not yet awaiting; the driver starts them now and resolves them
    when a later step awaits them. Every call appears in ``started`` or ``awaiting`` of the step
    it was made in, and an awaited one may already have been started.

    A finished script has ``output`` (its final value), or ``error`` when it failed: a syntax or
    type error, an uncaught exception, or one of Monty's own limits. ``aborted`` marks a step
    the sandbox lost in a way that may not recur on replay, with ``timed_out`` telling the
    watchdog apart from any other crash of the worker."""

    awaiting: list[Awaitable] = field(default_factory=list)
    started: list[HostCall | Sleep] = field(default_factory=list)
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
    ``OS_POLICY``) and must be deterministic under replay. File operations on ``mounts`` end the
    step (see :mod:`.vfs`). Not thread-safe; one run per script."""

    def __init__(
        self,
        script: str,
        stubs: TypeCheckStubs,
        *,
        answer_os: OsAnswer,
        mounts: Sequence[VFSMount] = (),
    ) -> None:
        self._script = script
        self._stubs = stubs
        self._answer_os = answer_os
        self._mounts = tuple(mounts)
        self._calls: dict[int, HostCall | Sleep] = {}
        self._made: list[int] = []  # ids of calls made since the last step ended
        self._dump: bytes | None = None
        self.stdout = monty.CollectString()

    def start(self, *, replaying: bool = False) -> Step:
        """Type-check and start the script, running it to its first awaited batch or done."""

        def begin(session: monty.MontySession) -> Any:
            return session.feed_start(self._script, print_callback=self.stdout)

        return self._step(begin, replaying)

    def resume(self, results: Mapping[int, Any], *, replaying: bool = False) -> Step:
        """Resume what the last step left the script awaiting with ``results`` (call id ->
        return value, or the exception the call failed with, which the script sees raised at
        its ``await`` or file operation), then run on to the next stop or done. Safe to repeat
        after an aborted attempt: it restores the same dump.

        Monty raises a built-in exception type as itself and any other as ``Exception``, with
        the same message."""

        def restore(session: monty.MontySession) -> Any:
            if self._dump is None:
                raise RuntimeError("ScriptRun.resume called before the script awaited anything")
            snap = session.load_snapshot(self._dump, print_callback=self.stdout)
            if isinstance(snap, monty.FutureSnapshot):
                return snap.resume(
                    {call_id: _settled(value) for call_id, value in results.items()}
                )
            if isinstance(snap, monty.FunctionSnapshot) and snap.is_os_function:
                return snap.resume(_settled(_to_monty(results[snap.call_id])))
            raise RuntimeError(f"expected a suspended script, restored {type(snap).__name__}")

        return self._step(restore, replaying)

    def _step(self, begin: Callable[[monty.MontySession], Any], replaying: bool) -> Step:
        pool = _replay_pool() if replaying else _live_pool()
        checkout = pool.checkout(
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
        """Run the session forward, deferring each host call as a future, until it awaits or
        reaches a file operation."""
        while True:
            if isinstance(snap, monty.MontyComplete):
                return Step(done=True, output=snap.output)
            if isinstance(snap, monty.FunctionSnapshot):
                if snap.is_os_function:
                    answered = self._os_call(snap)
                    if isinstance(answered, FileOp):
                        self._dump = snap.dump()
                        return self._stop([answered])
                    snap = answered
                elif snap.function_name in self._stubs.type_names:
                    try:
                        value = dict(*snap.args, **snap.kwargs)
                    except (TypeError, ValueError) as e:
                        snap = snap.resume({"exception": e})
                    else:
                        snap = snap.resume({"return_value": value})
                else:
                    self._record(
                        HostCall(
                            call_id=snap.call_id,
                            function_name=str(snap.function_name),
                            args=list(snap.args),
                            kwargs=dict(snap.kwargs),
                        )
                    )
                    snap = snap.resume({"future": ...})
                continue
            if isinstance(snap, monty.FutureSnapshot):
                self._dump = snap.dump()
                # Monty lists the pending ids in an order that differs from process to process;
                # ids are assigned in call order, so sorting makes the batch replay the same.
                pending = sorted(snap.pending_call_ids)
                return self._stop([self._calls.pop(i) for i in pending])
            # NameLookupSnapshot: leave the name undefined so the script raises NameError.
            snap = snap.resume()

    def _record(self, call: HostCall | Sleep) -> None:
        self._calls[call.call_id] = call
        self._made.append(call.call_id)

    def _stop(self, awaiting: list[Awaitable]) -> Step:
        """End the step blocked on ``awaiting``, handing over the calls made during it."""
        blocked = {item.call_id for item in awaiting}
        started = [self._calls[i] for i in self._made if i not in blocked and i in self._calls]
        self._made = []
        return Step(awaiting=awaiting, started=started)

    def _os_call(self, snap: monty.FunctionSnapshot) -> Any:
        """Answer one of the session's OS calls, leave it unhandled, or return the
        :class:`FileOp` the step stops at."""
        name = snap.function_name
        if name in FILE_OPERATIONS and self._mounts:
            return self._file_call(snap)
        if name == "asyncio.sleep":
            self._record(Sleep(call_id=snap.call_id, seconds=float(snap.args[0])))
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

    def _file_call(self, snap: monty.FunctionSnapshot) -> Any:
        """Route a file operation: answer what needs no backend, or return its FileOp."""
        name = str(snap.function_name)
        path = _path_arg(snap.args[0])
        routed = route(self._mounts, path)
        if routed is None:
            return snap.resume_not_handled()
        mount, relative = routed
        if name == "Path.is_symlink":
            return snap.resume({"return_value": False})
        if name in ("Path.resolve", "Path.absolute"):
            return snap.resume({"return_value": str(path)})
        if mount.read_only and is_write(name, snap.args):
            return snap.resume({"exception": _os_error(PermissionError, errno.EACCES, path)})
        args = tuple(snap.args[1:])
        target = None
        if name == "Path.rename":
            destination = _path_arg(snap.args[1])
            routed_target = route(self._mounts, destination)
            if routed_target is None or routed_target[0] is not mount:
                return snap.resume({"exception": _os_error(OSError, errno.EXDEV, destination)})
            target, args = routed_target[1], ()
        return FileOp(
            call_id=snap.call_id,
            operation=name,
            mount=mount,
            path=relative,
            virtual=path,
            args=args,
            kwargs=dict(snap.kwargs),
            target=target,
        )


def _path_arg(arg: Any) -> PurePosixPath:
    """A file operation's path, given as a path or as the handle of a file the script opened."""
    if isinstance(arg, monty.MontyFileHandle):
        return PurePosixPath(arg.path)
    return PurePosixPath(str(arg))


def _os_error(kind: type[OSError], code: int, path: PurePosixPath) -> OSError:
    return kind(code, os.strerror(code), str(path))


def _settled(value: Any) -> monty.ExternalSettledResult:
    return {"exception": value} if isinstance(value, Exception) else {"return_value": value}


def _to_monty(value: Any) -> Any:
    """A file operation's result in the form Monty expects. ``mtime`` is always explicit:
    Monty's stat helpers would otherwise read the worker's clock."""
    if isinstance(value, FileStat):
        if value.is_dir:
            return monty.StatResult.dir_stat(mtime=value.mtime)
        return monty.StatResult.file_stat(value.size, mtime=value.mtime)
    if isinstance(value, Opened):
        return monty.MontyFileHandle(value.path, value.mode)
    return value


def type_check(script: str, stubs: TypeCheckStubs) -> str | None:
    """Check ``script`` against ``stubs`` exactly as :meth:`ScriptRun.start` does, without
    running it.

    Returns the checker's syntax or type errors, each with its line, or ``None`` when the script
    is clean. Monty has no check-only entry point, but ``feed_start`` checks the snippet before
    executing any of it, so a clean script is started and then discarded: it runs only until its
    first suspension (or a brief time limit), no host call it makes is ever answered, and how far
    it got is never looked at, so the report depends on the checker alone.

    A worker that crashes says nothing about the script, so its ``MontyCrashedError``
    propagates rather than becoming an answer a replay might not reproduce: it fails the workflow
    task, and Temporal retries it."""
    checkout = _replay_pool().checkout(
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
