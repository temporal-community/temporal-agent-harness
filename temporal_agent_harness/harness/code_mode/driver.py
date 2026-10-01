"""The workflow-safe host-call driver that runs one Code Mode script to completion.

Runs a model-authored Python script in the sandbox by alternating two durable activities:
``code_start_batch`` compiles and runs the script to its first awaited batch of host calls;
then, for each batch, the driver runs every call CONCURRENTLY as its own durable activity and
feeds the results into ``code_resume_batch``, which resumes the script to the next batch. This
repeats until the script finishes, and its stdout + final value (or a script error) is returned
as text.

The host-call surface is generic: the driver holds a ``{name: tool}`` map and dispatches each
call the script makes to the matching harness tool via ``runner.run_tool`` — so every host call
inherits that tool's approval policy and tool_start/tool_end lifecycle. A call the policy refuses
is not a failure of the script: the script resumes with ``PermissionError`` raised at that call's
``await``, and the rest of its batch delivers normally.

Sandbox-safe: this module imports only ``pydantic`` + ``temporalio`` + the workflow-safe
:mod:`.batch_models`. It never imports ``pydantic_monty`` — the sandbox engine runs only in the
worker-side ``code_start_batch`` / ``code_resume_batch`` activities, which the driver dispatches
BY NAME (``CODE_START_BATCH_ACTIVITY`` / ``CODE_RESUME_BATCH_ACTIVITY``). A worker that hosts
Code Mode needs the ``code-mode`` extra installed, but merely importing this module (and thus
``harness.agent``) does not.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, TypeAdapter
from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_workflow import (
        AgentWorkflowRunner,
        ToolApprovalDenied,
    )

    from .batch_models import (
        CODE_RESUME_BATCH_ACTIVITY,
        CODE_START_BATCH_ACTIVITY,
        CallResult,
        CodeBatchStep,
        ResumeBatchInput,
        TypeCheckStubs,
    )


def _to_sandbox(value: Any) -> Any:
    """Render a tool's return value as plain JSON-native data the sandbox can index into.

    Pydantic models → ``model_dump(mode="json")`` (so ``datetime``/``enum``/``UUID``/``Decimal``
    become their JSON scalars, matching the generated type-check stubs); lists/tuples and dicts
    recurse; everything else passes through. ``mode="json"`` is deliberate — the value re-crosses
    into the resume activity (``CallResult.return_value``) and then into the Monty sandbox, both
    of which want JSON-native data, not Python objects."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_to_sandbox(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_sandbox(v) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class HostCallResult:
    """One host call that ran to completion: what the script asked for, and what it got back.

    Handed to a ``code_mode_tool``'s ``on_host_call_result`` observer, in-workflow, as each
    call returns. ``tool_id`` is the id the call ran under — the same one its approval gate
    and its ``tool_start``/``tool_end`` events carry. ``tool_input`` and ``result`` are
    JSON-native (models dumped, as the script itself sees them). A call the approval policy
    refused, or that raised, never produces one."""

    tool_id: str
    tool_name: str
    tool_input: dict[str, Any]
    result: Any


HostCallObserver = Callable[[HostCallResult], None]


@dataclass(frozen=True)
class _Refused:
    """A host call the approval policy refused, carrying the refusal for the script."""

    message: str


class CodeModeDriver:
    """Runs a Code Mode script to completion, dispatching its host calls to real harness tools.

    Constructed per invocation by the ``code_mode_tool`` closure (which resolves the live
    :class:`AgentWorkflowRunner` from the ambient ``_CURRENT_RUNNER``). ``tools_by_name`` and
    ``coercers`` are precomputed once by the factory (validated + type-adapters built at
    ``@agent.init``); ``stubs`` are the auto-generated stubs the sandbox type-checks the script
    against before running it. Never runs the sandbox engine itself — that
    happens in the ``code_start_batch`` / ``code_resume_batch`` activities."""

    def __init__(
        self,
        runner: AgentWorkflowRunner,
        tools_by_name: Mapping[str, Callable[..., Awaitable[Any]]],
        coercers: Mapping[str, Mapping[str, TypeAdapter[Any]]],
        *,
        injections: Mapping[str, Any],
        stubs: TypeCheckStubs,
        step_timeout: timedelta,
        on_host_call_result: HostCallObserver | None = None,
    ) -> None:
        self._runner = runner
        self._tools_by_name = tools_by_name
        self._coercers = coercers
        self._injections = injections
        self._stubs = stubs
        self._step_timeout = step_timeout
        self._on_host_call_result = on_host_call_result

    async def run_script(self, script: str) -> str:
        """Drive ``script`` to completion via the async batch loop; return its stdout + final
        value (or the error, as plain text).

        Each ``CodeBatchStep`` is a set of host calls the script is awaiting together (one
        ``await``, or several via ``asyncio.gather``); they run CONCURRENTLY as durable
        activities, so a script that gathers independent calls genuinely parallelizes them. The
        auto-generated ``stubs`` are passed so the sandbox type-checks the script
        first — a bad call comes back as ``error`` for the author to fix, not a mid-run failure."""
        log = workflow.logger
        log.info(
            "code_mode: starting async batch run (script_len=%d)\n--- script ---\n%s\n--- end ---",
            len(script),
            script,
        )

        stdout_parts: list[str] = []
        step: CodeBatchStep = await workflow.execute_activity(
            CODE_START_BATCH_ACTIVITY,
            args=[script, self._stubs],
            result_type=CodeBatchStep,
            start_to_close_timeout=self._step_timeout,
        )
        stdout_parts.append(step.stdout)

        batch_no = 0
        while not step.done:
            batch_no += 1
            log.info(
                "code_mode: batch %d — running %d host call(s) concurrently: %s",
                batch_no,
                len(step.pending),
                [c.function_name for c in step.pending],
            )
            # Run the whole awaited batch CONCURRENTLY — each host call is its own durable
            # activity (dispatched via run_tool, so each is independently approval-gated and
            # publishes its own tool lifecycle). Order is preserved to key results by call_id.
            # A refused call comes back as a value, not an exception, so a refusal cannot
            # abandon the rest of the batch mid-flight: every sibling runs to its own end and
            # the script hears about each outcome at its own await.
            results = await asyncio.gather(
                *(
                    self._dispatch_host_call(c.function_name, c.args, c.kwargs)
                    for c in step.pending
                )
            )
            results_input = [
                CallResult(call_id=c.call_id, permission_denied=r.message)
                if isinstance(r, _Refused)
                else CallResult(call_id=c.call_id, return_value=r)
                for c, r in zip(step.pending, results)
            ]
            step = await workflow.execute_activity(
                CODE_RESUME_BATCH_ACTIVITY,
                ResumeBatchInput(
                    snapshot=step.snapshot, results=results_input, type_names=self._stubs.type_names
                ),
                result_type=CodeBatchStep,
                start_to_close_timeout=self._step_timeout,
            )
            stdout_parts.append(step.stdout)

        if step.error:
            log.warning("code_mode: script error: %s", step.error)
            return f"Script error ({step.error})"

        output = json.loads(step.output_json) if step.output_json else None
        parts: list[str] = []
        combined = "".join(stdout_parts).strip()
        if combined:
            parts.append(f"output:\n{combined}")
        parts.append(f"result: {output!r}")
        log.info("code_mode: run complete after %d batch(es); returning reply", batch_no)
        return "\n".join(parts)

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
        sandbox, and reported to the ``on_host_call_result`` observer if one is wired.

        A call the approval policy refuses returns :class:`_Refused` rather than raising, so the
        driver can hand the refusal to the script as ``PermissionError``."""
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
        tool_id = str(workflow.uuid4())
        try:
            result = _to_sandbox(
                await self._runner.run_tool(
                    tool_id, tool, injections=self._injections, **coerced
                )
            )
        except ToolApprovalDenied as e:
            return _Refused(str(e))
        if self._on_host_call_result is not None:
            self._on_host_call_result(
                HostCallResult(
                    tool_id=tool_id,
                    tool_name=name,
                    tool_input=_to_sandbox(coerced),
                    result=result,
                )
            )
        return result
