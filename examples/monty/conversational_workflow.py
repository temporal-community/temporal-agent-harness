"""Conversational Monty agent: chat in text, and the model writes + runs its own scripts.

This is a demo-oriented twist on :class:`MontyDynamicAgentWorkflow`. That agent receives a
pre-written script per turn and runs it. This one puts a *model in the loop*: the user
chats in plain text, the model converses to gather what it needs, and when it decides it's
ready it **writes its own Python script and calls the ``run_monty_script`` tool** to execute
it in the Monty sandbox. The model then reads the script's output and replies in prose.

The Monty side is IDENTICAL to :class:`MontyDynamicAgentWorkflow`: the same async batch
driver (``monty_start_batch`` → ``monty_resume_batch``) backed by the same durable travel
activities (``_dispatch_host_call`` / ``_run_activity_tool``). The only new part is the
conversational front end — a Gemini Interactions tool-calling loop exposing a single tool,
``run_monty_script``.

This agent uses only a custom *function* tool (not the built-in ``file_search``), which
chains cleanly across turns via ``previous_interaction_id`` — so multi-turn conversation
works.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from functools import partial
from typing import Any

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from google.genai._interactions.types import (
        ErrorEvent,
        FunctionCallStep,
        InteractionCompletedEvent,
        StepDelta,
        StepStart,
        ToolParam,
    )
    from google.genai._interactions.types.error_event import Error
    from google.genai._interactions.types.function_result_step_param import (
        FunctionResultStepParam,
    )
    from google.genai._interactions.types.interaction_create_params import Input
    from google.genai._interactions.types.step_delta import (
        DeltaArgumentsDelta,
        DeltaText,
    )
    from google.genai.client import AsyncClient
    from temporal_agent_harness.ai_sdks.google_genai_plugin import function_param, google_genai_client
    from temporal_agent_harness.harness import agent, slash_commands
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        SlashCommand,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from ._host_driver import MontyHostDriver


TASK_QUEUE = "monty-dynamic-agent"
SUPPORTED_MODELS = ("gemini-3.5-flash", "gemini-3.1-flash-lite")
DEFAULT_MODEL = SUPPORTED_MODELS[0]


def model_slash_command(set_model) -> slash_commands.SlashCommandDefinition:
    return slash_commands.model_selector(
        choices=SUPPORTED_MODELS,
        set_model=set_model,
        description="Set the model for this Monty session.",
    )


# The script-writing contract the model must follow. The host functions are ASYNC — the
# script awaits them, and the runtime executes each as a durable Temporal activity. Calls
# the script `await`s together via `asyncio.gather` run CONCURRENTLY, so the model is
# pushed to parallelize independent work. See travel_models.HOST_FUNCTION_STUBS (the typed
# stubs the script is type-checked against) and the workflow's batch driver.
_SCRIPT_CONTRACT = """\
You can RUN PYTHON by calling the `run_monty_script` tool with a `script` string. The script \
runs in the Monty sandbox: no filesystem, no network, no arbitrary imports — just ordinary \
in-sandbox Python (arithmetic, comprehensions, f-strings, `print`) plus `asyncio` and the \
host functions below. The value of the script's LAST EXPRESSION becomes the tool result \
(along with anything printed).

The host functions are ASYNC — you MUST `await` them. Structure every script as:

    import asyncio
    async def main():
        ...        # await host functions here
        return <final value>
    asyncio.run(main())

CONCURRENCY (important): independent host calls should run AT THE SAME TIME with \
`asyncio.gather` — the runtime executes a gathered batch concurrently, so don't await them \
one-by-one when they don't depend on each other. Only await sequentially when a later call \
needs an earlier call's result (e.g. you must search before you can book).

Your script is STATICALLY TYPE-CHECKED against the host-function signatures below before it \
runs. Forgetting `await`, passing a wrong argument type, or reading a result key that \
doesn't exist all come back as a type error instead of a result — read it and fix the \
script. The result keys listed for each function are exact; only those keys exist.

Host functions (all `async`, all must be awaited):

  • async search_flights(origin: str, destination: str, date: str) -> list[dict]
        Flights between two airport codes on a date ("YYYY-MM-DD"). Each dict:
        flight_id, airline, departure_time ("HH:MM"), arrival_time ("HH:MM"),
        price_usd (float), stops (int).
  • async search_hotels(city: str, check_in: str, check_out: str) -> list[dict]
        Hotels in a city for a date range. Each dict: hotel_id, name, star_rating (int),
        price_per_night_usd (float), neighborhood (str).
  • async book_flight(flight_id: str, passenger_name: str) -> dict
        Returns: confirmation_code, flight_id, passenger_name, status.
  • async book_hotel(hotel_id: str, guest_name: str) -> dict
        Returns: confirmation_code, hotel_id, guest_name, status.
  • async get_trip_summary(booking_refs: list[str]) -> str
        Human-readable itinerary from confirmation codes.

Index results with normal Python (e.g. flights[0]["price_usd"], min(...)). Bind any values \
you need as literals in the script — there are no inputs.

Example script (note the concurrent search, then the dependent bookings):
    import asyncio
    async def main():
        # independent searches run concurrently
        flights, hotels = await asyncio.gather(
            search_flights("SFO", "JFK", "2026-07-01"),
            search_hotels("New York", "2026-07-01", "2026-07-05"),
        )
        cheapest = min(flights, key=lambda f: f["price_usd"])
        nicest = max(hotels, key=lambda h: h["star_rating"])
        # these depend on the searches, but are independent of each other -> gather
        flight, hotel = await asyncio.gather(
            book_flight(cheapest["flight_id"], "Ada Lovelace"),
            book_hotel(nicest["hotel_id"], "Ada Lovelace"),
        )
        print(f"booked {cheapest['airline']} at ${cheapest['price_usd']}")
        return await get_trip_summary([flight["confirmation_code"], hotel["confirmation_code"]])
    asyncio.run(main())
"""

SYSTEM_INSTRUCTION = f"""\
You are a friendly travel-booking assistant. You help users search and book flights and \
hotels and assemble trip itineraries. You don't have these abilities directly — instead you \
write small **async** Python scripts and execute them with the `run_monty_script` tool. Every \
script MUST be async: the host functions are coroutines you `await`, you run independent ones \
concurrently with `asyncio.gather`, and you wrap the body in `asyncio.run(main())` (full rules \
below).

{_SCRIPT_CONTRACT}

How to behave:
- Converse naturally. Ask brief clarifying questions when you're missing something essential \
(origin/destination, dates, traveler name) — don't interrogate; make reasonable assumptions \
and state them.
- When you have enough to make progress, WRITE A SCRIPT and call `run_monty_script`. Keep \
each script focused (search, or book, or summarize) so you can react to results.
- After a tool result, read it and reply to the user in plain, friendly prose — summarize \
options, prices, confirmations. You may run more scripts in follow-up turns as the \
conversation continues.
- Never invent flight_ids/hotel_ids/confirmation codes — only use ones returned by a script."""


@workflow.defn(name="MontyChatAgent")
@agent.defn
class MontyChatAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Demo stance: require human approval for EVERY tool call — both the
            # `run_monty_script` tool and each host call the script makes (search/book
            # flights & hotels), since every call is dispatched through run_tool and gated.
            # always_require_approvals does not auto-approve even inherently_safe tools.
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            slash_commands=[
                *slash_commands.default_commands(),
                model_slash_command(self._set_model),
            ],
        )
        self._model: str = DEFAULT_MODEL
        # Server-side conversation chaining id (Interactions API); updated each turn. Safe to
        # chain here because this agent uses only a function tool (no file_search).
        self._previous_interaction_id: str | None = None
        # Shared execution half: runs the model-authored script via the async batch loop
        # (composition — same driver the script-only MontyDynamicAgent uses).
        self._monty = MontyHostDriver(self._runner)
        # The single model-facing tool: an inline workflow tool that runs a model-authored
        # script through the Monty async batch driver. Built once, closing over `self`.
        self._monty_tool = self._build_monty_tool()

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        # The Temporal-aware AsyncClient from the Gemini plugin; the runner is wired in so reply
        # text streams to the workflow stream as it is generated.
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(minutes=3),
            ),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the travel assistant. Describe the trip you want (flights, hotels,
        dates, traveler name) in plain text; the assistant converses, writes and runs Python
        scripts against a simulated travel backend as needed, and replies with the results."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    @agent.accepts
    async def slash(self, command: SlashCommand) -> TextReply:
        """Apply a slash command to this parent agent session."""
        return TextReply(
            text=(
                f"Unknown Monty slash command: `{command.name}`. Try `/model`. "
                "Harness commands include `/approvals`, `/allow-tools`, and `/status`."
            )
        )

    def _set_model(self, model: str) -> None:
        self._model = model

    # ------------------------------------------------------------------ chat loop

    def _build_monty_tool(self) -> Any:
        """Build the ``run_monty_script`` inline tool, closing over this workflow instance.

        It's an ``@agent.tool_defn`` so it runs IN the workflow (the Monty async batch
        loop must orchestrate durable activities) and publishes its own tool lifecycle. The
        docstring is the model-facing contract."""

        @agent.tool_defn(inherently_safe=True)
        async def run_monty_script(script: str) -> str:
            return await self._monty.run_script(script)

        # The model reads this; keep it aligned with _SCRIPT_CONTRACT.
        run_monty_script.__doc__ = (
            "Execute a Python `script` in the Monty sandbox and return its printed output "
            "and final value. Use this to search/book flights and hotels and build "
            "itineraries via the host functions (search_flights, search_hotels, book_flight, "
            "book_hotel, get_trip_summary). The script's LAST EXPRESSION is returned. See the "
            "system instructions for the full sandbox contract and host-function signatures."
        )
        return run_monty_script

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: stream the model, dispatch any ``run_monty_script``
        calls, feed results back, and loop until the model replies with no further calls.

        Updates ``self._previous_interaction_id`` for chaining the next turn (no file_search
        here, so chaining is safe)."""
        tools = [function_param(self._monty_tool)]
        next_input: Input = user_text
        while True:
            (
                reply_text,
                pending_calls,
                self._previous_interaction_id,
            ) = await self._execute_agent_interaction(
                gemini=gemini,
                model=self._model,
                input=next_input,
                tools=tools,
                system_instruction=SYSTEM_INSTRUCTION,
                previous_interaction_id=self._previous_interaction_id,
            )

            if not pending_calls:
                return reply_text

            next_input = await asyncio.gather(
                *(self._run_one_tool(fc) for fc in pending_calls)
            )

    async def _run_one_tool(self, call: FunctionCallStep) -> FunctionResultStepParam:
        """Execute one ``run_monty_script`` call via ``run_tool`` and return its result.

        ``run_tool`` parks the call id so the script's host calls and the tool's own
        lifecycle events correlate with the streaming activity's ``tool_requested``."""
        try:
            if call.name != self._monty_tool.__name__:
                raise ValueError(f"unknown tool: {call.name!r}")
            result = await self._runner.run_tool(
                call.id, self._monty_tool, **call.arguments
            )
            response: FunctionResultStepParam = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": str(result),
            }
            if call.signature:
                response["signature"] = call.signature
            return response
        except Exception as e:
            response = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": str(e),
                "is_error": True,
            }
            if call.signature:
                response["signature"] = call.signature
            return response

    async def _execute_agent_interaction(
        self,
        *,
        gemini: AsyncClient,
        model: str,
        input: Input,
        tools: list[ToolParam],
        system_instruction: str,
        previous_interaction_id: str | None,
    ) -> tuple[str, list[FunctionCallStep], str]:
        """Stream one ``interactions.create`` and reduce it into actionable state.

        Returns ``(reply_text, function_calls, interaction_id)``. Text comes from
        ``DeltaText`` events; function calls are captured from each ``StepStart`` whose step
        is a ``FunctionCallStep``, with their JSON-string ``arguments`` fragments buffered per
        step index and ``json.loads``-ed once the stream ends. (Lifted verbatim from the QA
        agent's loop.) Raises :class:`ApplicationError` on stream errors or if the stream
        ends without a completed event."""
        interactions_create_fn = partial(
            gemini.interactions.create,
            model=model,
            input=input,
            system_instruction=system_instruction,
            tools=tools,
            stream=True,
        )
        if previous_interaction_id:
            stream = await interactions_create_fn(
                previous_interaction_id=previous_interaction_id
            )
        else:
            stream = await interactions_create_fn()

        text_parts: list[str] = []
        calls_by_index: dict[int, FunctionCallStep] = {}
        arg_buffers: dict[int, str] = {}
        interaction_id: str | None = None
        async for event in stream:
            match event:
                case ErrorEvent(error=Error(message=msg, code=code)):
                    raise ApplicationError(
                        msg or "stream error", type=code or "stream_error"
                    )
                case ErrorEvent():
                    raise ApplicationError("unknown stream error", type="stream_error")
                case StepStart(index=idx, step=FunctionCallStep() as call):
                    calls_by_index[idx] = call
                case StepDelta(
                    index=idx, delta=DeltaArgumentsDelta(arguments=args)
                ) if args:
                    arg_buffers[idx] = arg_buffers.get(idx, "") + args
                case StepDelta(delta=DeltaText(text=text)) if text:
                    text_parts.append(text)
                case InteractionCompletedEvent(interaction=interaction):
                    interaction_id = interaction.id

        if interaction_id is None:
            raise ApplicationError(
                "stream ended without interaction.completed event",
                type="stream_error",
            )

        function_calls = [
            calls_by_index[idx].model_copy(
                update={"arguments": json.loads(arg_buffers[idx])}
            )
            if arg_buffers.get(idx)
            else calls_by_index[idx]
            for idx in sorted(calls_by_index)
        ]
        return "".join(text_parts), function_calls, interaction_id

    # Monty execution (the async batch loop + host-call dispatch) lives in the shared
    # MontyHostDriver held in self._monty (composition); call self._monty.run_script(...).
