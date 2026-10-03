"""The workflow-side host-call driver that runs one Code Mode script to completion.

Runs a model-authored Python script in the sandbox, stepping it from workflow code (see
:mod:`.monty_stepper` for why that is durable): the first step type-checks the script and runs
it to its first awaited batch of host calls; the driver then runs every call in the batch
CONCURRENTLY as its own durable activity and resumes the script with their results, until the
script finishes. Its stdout + final value (or a script error) is returned as text.

The host-call surface is generic: the driver holds a ``{name: tool}`` map and dispatches each
call the script makes to the matching harness tool via ``runner.run_tool`` — so every host call
inherits that tool's approval policy and tool_start/tool_end lifecycle.

=== Replay-safe step limits ===

A step that runs too long between host interactions is killed (see :mod:`.monty_stepper`). Whether that happens depends on the machine, so it must not be decided
again on replay. When a step is aborted on its original run, the driver records a patch marker
naming the script and step, and returns an error. On replay, it checks for that marker before
running the step: if present, it returns the same error without running the step; if absent,
the step completed originally and is re-run with only a generous backstop limit. A skipped step
must leave no trace in what the workflow does next, so the script draws entropy from its own
generator rather than the workflow's, and an error reply carries no stdout.

Sandbox-safe: this module never imports ``pydantic_monty`` at module scope. The stepping engine
is imported through the sandbox's pass-through by :func:`load_stepper`, which ``code_mode_tool``
calls at construction so a worker without the ``code-mode`` extra fails the workflow task (and
is retried until a worker with it picks the task up) instead of failing a tool call.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from types import ModuleType
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .stubs import TypeCheckStubs

_T = TypeVar("_T")


def load_stepper() -> ModuleType:
    """Import the sandbox engine, :mod:`.monty_stepper`, or explain the missing extra."""
    try:
        with workflow.unsafe.imports_passed_through():
            # Absolute, so the sandbox passes the module through by name.
            import temporal_agent_harness.harness.code_mode.monty_stepper as monty_stepper
    except ImportError as e:
        raise RuntimeError(
            "Code Mode needs the harness's `code-mode` extra, which provides pydantic-monty — "
            "the sandbox Code Mode scripts run in. Install temporal-agent-harness[code-mode] on "
            "every worker that runs this agent."
        ) from e
    return monty_stepper


def call_monty(fn: Callable[..., _T], *args: Any) -> _T:
    """Call into the sandbox engine with imports passed through the workflow sandbox: Monty
    imports ``opentelemetry`` lazily, from native code, on its first run."""
    with workflow.unsafe.imports_passed_through():
        return fn(*args)


def _to_sandbox(value: Any) -> Any:
    """Render a tool's return value as plain JSON-native data the sandbox can index into.

    Pydantic models → ``model_dump(mode="json")`` (so ``datetime``/``enum``/``UUID``/``Decimal``
    become their JSON scalars, matching the generated type-check stubs); lists/tuples and dicts
    recurse; everything else passes through."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_to_sandbox(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_sandbox(v) for k, v in value.items()}
    return value


class CodeModeDriver:
    """Runs a Code Mode script to completion, dispatching its host calls to real harness tools.

    Constructed per invocation by the ``code_mode_tool`` closure (which resolves the live
    :class:`AgentWorkflowRunner` from the ambient ``_CURRENT_RUNNER``). ``tools_by_name`` and
    ``coercers`` are precomputed once by the factory (validated + type-adapters built at
    ``@agent.init``); ``stubs`` are the auto-generated stubs the sandbox type-checks the script
    against before running it."""

    def __init__(
        self,
        runner: AgentWorkflowRunner,
        tools_by_name: Mapping[str, Callable[..., Awaitable[Any]]],
        coercers: Mapping[str, Mapping[str, TypeAdapter[Any]]],
        *,
        injections: Mapping[str, Any],
        stubs: TypeCheckStubs,
    ) -> None:
        self._runner = runner
        self._tools_by_name = tools_by_name
        self._coercers = coercers
        self._injections = injections
        self._stubs = stubs

    async def run_script(self, script: str) -> str:
        """Drive ``script`` to completion; return its stdout + final value (or the error, as
        plain text).

        Each step ends at a set of host calls the script is awaiting together (one ``await``,
        or several via ``asyncio.gather``); they run CONCURRENTLY as durable activities, so a
        script that gathers independent calls genuinely parallelizes them. The sandbox
        type-checks the script first, so a bad call comes back as an error for the author to
        fix, not a mid-run failure."""
        stepper = load_stepper()
        script_id = str(workflow.uuid4())
        entropy = random.Random(workflow.random().getrandbits(64))
        run = stepper.ScriptRun(
            script, self._stubs, answer_os=lambda name, args: _answer_os(name, args, entropy)
        )

        step = self._step(script_id, 0, lambda replaying: run.start(replaying=replaying))
        step_no = 0
        while not step.done:
            step_no += 1
            # Run the whole awaited batch CONCURRENTLY — each host call is its own durable
            # activity (dispatched via run_tool, so each is independently approval-gated and
            # publishes its own tool lifecycle). A call that fails is raised inside the script at
            # its await, where the script may catch it; the rest of the batch still completes.
            outcomes = await asyncio.gather(
                *(self._run_call(call, stepper) for call in step.awaiting), return_exceptions=True
            )
            results = {
                call.call_id: _script_outcome(outcome)
                for call, outcome in zip(step.awaiting, outcomes)
            }
            step = self._step(
                script_id, step_no, lambda replaying: run.resume(results, replaying=replaying)
            )

        if step.error:
            return f"Script error ({step.error})"

        parts: list[str] = []
        combined = run.stdout.output.strip()
        if combined:
            parts.append(f"output:\n{combined}")
        parts.append(f"result: {_render(step.output)!r}")
        return "\n".join(parts)

    def _step(self, script_id: str, step_no: int, advance: Callable[[bool], Any]) -> Any:
        """Run one step under the replay-safe limit described in the module docstring."""
        timed_out_marker = f"temporal-agent-harness/code-mode/timed-out/{script_id}/{step_no}"
        crashed_marker = f"temporal-agent-harness/code-mode/crashed/{script_id}/{step_no}"
        stepper = load_stepper()
        if workflow.unsafe.is_replaying():
            if workflow.patched(timed_out_marker):
                return stepper.Step(done=True, error=stepper.TIMED_OUT_ERROR)
            if workflow.patched(crashed_marker):
                return stepper.Step(done=True, error=stepper.CRASHED_ERROR)
            step = call_monty(advance, True)
            if step.aborted:
                # The step completed on its original run, so this outcome cannot be returned.
                # Fail the workflow task and let Temporal retry it.
                raise RuntimeError(
                    f"Code Mode script {script_id} step {step_no} completed on its original run "
                    f"but not on replay: {step.error}"
                )
            return step

        step = call_monty(advance, False)
        if not step.aborted:
            return step
        workflow.patched(timed_out_marker if step.timed_out else crashed_marker)
        workflow.logger.warning("code_mode: step %d aborted: %s", step_no, step.error)
        if step.timed_out:
            return stepper.Step(done=True, error=stepper.TIMED_OUT_ERROR)
        return stepper.Step(done=True, error=stepper.CRASHED_ERROR)

    async def _run_call(self, call: Any, stepper: ModuleType) -> Any:
        if isinstance(call, stepper.Sleep):
            await workflow.sleep(call.seconds)
            return None
        return await self._dispatch_host_call(call.function_name, call.args, call.kwargs)

    async def _dispatch_host_call(
        self, name: str | None, args: list[Any], kwargs: dict[str, Any]
    ) -> Any:
        """Map one suspended host call to its harness tool and run it durably via ``run_tool``.

        Looks the tool up by ``name``, binds the sandbox-supplied ``args``/``kwargs`` against the
        tool's model-facing signature, PRE-COERCES each argument into the tool's declared type
        (via a precomputed ``TypeAdapter`` — required because inline ``@agent.tool_defn`` tools do
        NOT coerce at a Temporal boundary the way activity tools do), then dispatches through
        ``runner.run_tool`` (which applies the tool's own approval policy + publishes its
        tool_start/tool_end lifecycle). The result is rendered to JSON-native data for the
        sandbox."""
        tool = self._tools_by_name.get(name) if name is not None else None
        if tool is None:
            # Defense in depth: type-checking against the generated stubs should already reject
            # an unknown bare name at compile time.
            raise ApplicationError(
                f"script called unknown host function {name!r}",
                type="UnknownHostFunction",
                non_retryable=True,
            )

        sig = inspect.signature(tool)
        try:
            bound = sig.bind(*args, **kwargs)
        except TypeError as e:
            raise ApplicationError(
                f"host call {name!r}: {e}",
                type="HostCallBinding",
                non_retryable=True,
            ) from e
        bound.apply_defaults()

        adapters = self._coercers.get(name, {})
        coerced: dict[str, Any] = {}
        for pname, value in bound.arguments.items():
            adapter = adapters.get(pname)
            coerced[pname] = adapter.validate_python(value) if adapter is not None else value

        # The script supplies only the model-facing arguments; the tool's Injected[...] parameters
        # are hidden from it and filled from the harness-owned injections here.
        result = await self._runner.run_tool(
            str(workflow.uuid4()), tool, injections=self._injections, **coerced
        )
        return _to_sandbox(result)


def _script_outcome(outcome: Any) -> Any:
    """What a host call's outcome is to the script: its value, or the exception it failed with.

    Cancellation is the workflow's, not the call's, so it propagates instead. A failed activity is
    unwrapped to its cause, which carries the tool's own message. The sandbox raises a built-in
    exception as its own type and anything else as ``Exception``, so a non-built-in keeps its
    type name in the message."""
    if not isinstance(outcome, BaseException):
        return outcome
    if not isinstance(outcome, Exception) or is_cancelled_exception(outcome):
        raise outcome
    error: BaseException = outcome
    if isinstance(error, ActivityError) and error.cause is not None:
        error = error.cause
    if type(error).__module__ == "builtins":
        return error
    return Exception(f"{type(error).__name__}: {error}")



def _answer_os(name: str, args: tuple[Any, ...], entropy: random.Random) -> Any:
    """Answer the session's clock, entropy and environment calls from workflow-deterministic
    sources. The sandbox's local zone is UTC, so naive times are UTC."""
    now = workflow.now()
    if name == "datetime.now":
        tz = args[0] if args else None
        return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)
    if name == "date.today":
        return now.date()
    if name == "time.time":
        return now.timestamp()
    if name == "os.urandom":
        return entropy.randbytes(args[0])
    if name == "os.getenv":
        return args[1] if len(args) > 1 else None
    if name == "os.environ":
        return {}
    raise NotImplementedError(name)


def _render(value: Any) -> Any:
    """The script's final value as JSON-native data, so the reply reads the same however Monty
    represents it."""
    return json.loads(json.dumps(value, default=str))
