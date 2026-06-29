"""Shared Monty host-call driver for both Monty agents.

Both :class:`MontyDynamicAgentWorkflow` (script-as-request) and the conversational Monty
workflows (LLM writes the script) run a model/caller-authored Python script the same way:
via the async batch driver in :mod:`.monty_activities`. They differ only in their front
door — how the script arrives — not in how it executes. This driver is that shared
execution half, used by **composition**: each agent workflow constructs one with
its :class:`AgentWorkflowRunner` and calls :meth:`MontyHostDriver.run_script`.

  * :meth:`run_script` — the batch loop: ``monty_start_batch`` once, then repeatedly run
    each awaited batch of host calls CONCURRENTLY as durable activities and feed the results
    into ``monty_resume_batch`` until the script completes.
  * :meth:`_dispatch_host_call` — the single source of truth mapping a sandbox host call
    (``search_flights``, ``book_hotel``, …) to its durable travel activity. (Previously
    duplicated in both workflows — the "keep in sync" hazard the stubs comment warned about.)
  * :meth:`_run_activity_tool` — run one host activity through ``run_tool`` so it publishes
    its own ``tool_start``/``tool_end`` lifecycle (and is approval-gated per the agent policy).
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from . import activities
    from .monty_activities import (
        CallResult,
        MontyBatchStep,
        MontyResumeBatchInput,
        monty_resume_batch,
        monty_start_batch,
    )
    from .travel_models import (
        HOST_FUNCTION_STUBS,
        FlightBookingRequest,
        FlightSearchRequest,
        HotelBookingRequest,
        HotelSearchRequest,
        TripSummaryRequest,
    )


# Ceiling for a single Monty batch step (compile/run-to-first-await, or resume-to-next-await).
_MONTY_STEP_TIMEOUT = timedelta(seconds=30)


class MontyHostDriver:
    """The async Monty batch loop + host-call dispatch, held by an agent workflow.

    Constructed with the agent's :class:`AgentWorkflowRunner`. Never runs Monty itself —
    that happens in the ``monty_*_batch`` activities; this only orchestrates, running each
    awaited batch of host calls concurrently."""

    def __init__(self, runner: AgentWorkflowRunner) -> None:
        self._runner = runner

    async def run_script(self, script: str) -> str:
        """Drive ``script`` to completion via the async batch loop; return its stdout + final
        value (or the error, as plain text).

        Each ``MontyBatchStep`` is a set of host calls the script is awaiting together (one
        ``await``, or several via ``asyncio.gather``); they run CONCURRENTLY as durable
        activities, so a script that gathers independent calls genuinely parallelizes them.
        ``HOST_FUNCTION_STUBS`` is passed so Monty type-checks the script first — a bad call
        comes back as ``error`` for the author to fix, rather than failing mid-run."""
        log = workflow.logger
        log.info(
            "monty: starting async batch run (script_len=%d)\n--- script ---\n%s\n--- end ---",
            len(script),
            script,
        )

        stdout_parts: list[str] = []
        step: MontyBatchStep = await workflow.execute_activity(
            monty_start_batch,
            args=[script, HOST_FUNCTION_STUBS],
            start_to_close_timeout=_MONTY_STEP_TIMEOUT,
        )
        stdout_parts.append(step.stdout)

        batch_no = 0
        while not step.done:
            batch_no += 1
            log.info(
                "monty: batch %d — running %d host call(s) concurrently: %s",
                batch_no,
                len(step.pending),
                [c.function_name for c in step.pending],
            )
            # Run the whole awaited batch CONCURRENTLY — each host call is its own durable
            # activity (dispatched via run_tool, so each is independently approval-gated and
            # publishes its own tool lifecycle). Order is preserved to key results by call_id.
            results = await asyncio.gather(
                *(
                    self._dispatch_host_call(c.function_name, c.args, c.kwargs)
                    for c in step.pending
                )
            )
            results_input = [
                CallResult(call_id=c.call_id, return_value=r)
                for c, r in zip(step.pending, results)
            ]
            step = await workflow.execute_activity(
                monty_resume_batch,
                MontyResumeBatchInput(snapshot=step.snapshot, results=results_input),
                start_to_close_timeout=_MONTY_STEP_TIMEOUT,
            )
            stdout_parts.append(step.stdout)

        if step.error:
            log.warning("monty: script error: %s", step.error)
            return f"Script error ({step.error})"

        output = json.loads(step.output_json) if step.output_json else None
        parts: list[str] = []
        combined = "".join(stdout_parts).strip()
        if combined:
            parts.append(f"output:\n{combined}")
        parts.append(f"result: {output!r}")
        log.info("monty: run complete after %d batch(es); returning reply", batch_no)
        return "\n".join(parts)

    async def _dispatch_host_call(
        self, name: str | None, args: list[Any], kwargs: dict[str, Any]
    ) -> Any:
        """Map a suspended script's host call to its durable travel activity and return the
        result as plain dicts/lists/strings the sandbox can index into.

        This is the single source of truth for the host-function surface (keep aligned with
        ``travel_models.HOST_FUNCTION_STUBS`` and the agents' script contracts)."""

        def bind(*names: str) -> dict[str, Any]:
            bound = dict(zip(names, args))
            bound.update(kwargs)
            return bound

        match name:
            case "search_flights":
                resp = await self._run_activity_tool(
                    activities.search_flights_activity,
                    FlightSearchRequest(**bind("origin", "destination", "date")),
                )
                return [f.model_dump() for f in resp.flights]
            case "search_hotels":
                resp = await self._run_activity_tool(
                    activities.search_hotels_activity,
                    HotelSearchRequest(**bind("city", "check_in", "check_out")),
                )
                return [h.model_dump() for h in resp.hotels]
            case "book_flight":
                resp = await self._run_activity_tool(
                    activities.book_flight_activity,
                    FlightBookingRequest(**bind("flight_id", "passenger_name")),
                )
                return resp.model_dump()
            case "book_hotel":
                resp = await self._run_activity_tool(
                    activities.book_hotel_activity,
                    HotelBookingRequest(**bind("hotel_id", "guest_name")),
                )
                return resp.model_dump()
            case "get_trip_summary":
                resp = await self._run_activity_tool(
                    activities.get_trip_summary_activity,
                    TripSummaryRequest(**bind("booking_refs")),
                )
                return resp.summary
            case _:
                raise ApplicationError(
                    f"script called unknown host function {name!r}",
                    type="UnknownHostFunction",
                    non_retryable=True,
                )

    async def _run_activity_tool(self, tool: Any, request: Any) -> Any:
        """Execute one ``@agent.activity_tool_defn`` host activity through ``run_tool`` so it
        publishes its own ``tool_start``/``tool_end`` lifecycle on the turn stream."""
        call_id = str(workflow.uuid4())
        return await self._runner.run_tool(call_id, tool, request)
