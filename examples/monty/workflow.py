"""Monty dynamic agent: each turn runs a caller-supplied Python script (no model in the loop).

A deliberately minimal harness agent. Each turn carries an arbitrary Python *script* (see
:class:`RunScript`) that runs in a sandbox whose only escape hatches are the travel host
functions — each backed by a durable Temporal activity. There is no model in the loop: the
"plan" arrives pre-written as the script.

It's a thin consumer of the harness Code Mode feature: :func:`agent.code_mode_tool` turns the
travel activity tools into a single run-a-script tool, and the ``run_script`` handler dispatches
the caller's script straight through it. The script calls the tools as async host functions;
each host call runs as a durable, approval-gated activity, and independent calls can run
concurrently via ``asyncio.gather``.

Contrast with the conversational Monty agent (``conversational_workflow.py``): that one drives
the Gemini Interactions API with a model in the loop, over the same Code Mode tool. This one has
no model — the script is the input. Both reuse the standard harness contract: ``@agent.defn`` +
an :class:`AgentWorkflowRunner` built in ``@workflow.init``, the turn loop driven by
``await runner.run(self)``, and a single ``@agent.accepts`` handler (``run_script``) whose return
value becomes the turn's reply.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import AgentConfig, TextReply, ToolApprovalPolicy
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from . import activities
    from .models import RunScript


# The agent's own task queue. The (agent-agnostic) session manager launches this agent by
# its registered defn name on whichever queue the caller specifies — so this agent runs
# under its true name on its own queue; no masquerading as another agent.
TASK_QUEUE = "monty-dynamic-agent"


@workflow.defn(name="MontyDynamicAgent")
@agent.defn
class MontyDynamicAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # The travel tools run inside a sandboxed simulation (no real-world side effects),
            # so this agent skips approvals by default. A caller can still tighten this per
            # session via AgentConfig.approval_policy.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # Code Mode over the travel tools: one tool that runs a script calling them as host
        # functions. The run_script handler dispatches the caller's script straight through it.
        self._run_code = agent.code_mode_tool(
            [
                activities.search_flights_activity,
                activities.search_hotels_activity,
                activities.book_flight_activity,
                activities.book_hotel_activity,
                activities.get_trip_summary_activity,
            ],
            name="run_travel_code",
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def run_script(self, message: RunScript) -> TextReply:
        r"""Execute a Python ``script`` in a sandbox over the travel host functions, returning its
        printed output + final value.

        The sandbox has no filesystem, network, or arbitrary imports — only ``asyncio`` plus the
        host functions below, each backed by a durable Temporal activity. Everything else is
        ordinary in-sandbox Python. The value of the script's LAST EXPRESSION becomes the reply
        (alongside anything ``print``ed), so end the script with the value you want back.

        The host functions are ``async`` — you MUST ``await`` them — so structure the script as::

            import asyncio
            async def main():
                ...                 # await host functions here
                return <final value>
            asyncio.run(main())

        Run INDEPENDENT host calls concurrently with ``asyncio.gather`` (a gathered batch runs
        concurrently, each call its own durable activity); only await sequentially when a later
        call needs an earlier call's result. Each host function takes ONE argument — a dict
        matching its request shape — and returns a dict; index results with normal Python
        (``resp["flights"][0]["price_usd"]``). Scripts are statically type-checked against these
        signatures before running, so a wrong argument shape or an unknown result key comes back
        as an error to fix rather than a result (a bad script is normal input, not a failure).

        Host functions (all ``async``, all must be awaited):

          • ``search_flights({"origin": str, "destination": str, "date": str}) -> dict``
                Returns ``{"flights": [flight, ...]}``, each flight: ``flight_id``, ``airline``,
                ``departure_time`` ("HH:MM"), ``arrival_time`` ("HH:MM"), ``price_usd`` (float),
                ``stops`` (int).
          • ``search_hotels({"city": str, "check_in": str, "check_out": str}) -> dict``
                Returns ``{"hotels": [hotel, ...]}``, each hotel: ``hotel_id``, ``name``,
                ``star_rating`` (int), ``price_per_night_usd`` (float), ``neighborhood`` (str).
          • ``book_flight({"flight_id": str, "passenger_name": str}) -> dict``
                Returns ``confirmation_code``, ``flight_id``, ``passenger_name``, ``status``.
          • ``book_hotel({"hotel_id": str, "guest_name": str}) -> dict``
                Returns ``confirmation_code``, ``hotel_id``, ``guest_name``, ``status``.
          • ``get_trip_summary({"booking_refs": [str, ...]}) -> dict``
                Returns ``{"summary": str}`` — a human-readable itinerary from confirmation codes.

        Example — concurrently search, then book, then summarize::

            import asyncio
            async def main():
                flights, hotels = await asyncio.gather(
                    search_flights({"origin": "SFO", "destination": "JFK", "date": "2026-07-01"}),
                    search_hotels({"city": "New York", "check_in": "2026-07-01", "check_out": "2026-07-05"}),
                )
                cheapest = min(flights["flights"], key=lambda f: f["price_usd"])
                flight = await book_flight({"flight_id": cheapest["flight_id"], "passenger_name": "Ada Lovelace"})
                summary = await get_trip_summary({"booking_refs": [flight["confirmation_code"]]})
                return summary["summary"]
            asyncio.run(main())
        """
        result = await self._runner.run_tool(
            str(workflow.uuid4()), self._run_code, script=message.script
        )
        return TextReply(text=result)
