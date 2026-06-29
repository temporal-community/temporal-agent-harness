"""Gemini conversational Monty agent: chat in text, and the model writes scripts.

This is the Gemini counterpart to ``conversational_workflow.py``. It exposes the same
Monty capability to the user, but drives the model through the Gemini Interactions API
instead of the OpenAI Agents SDK. Keeping it as a separate workflow makes the provider
differences explicit:

* Gemini uses ``google_genai_client().interactions.create(stream=True)`` from workflow code.
* The workflow owns the function-calling loop: it reads streamed function-call steps, runs
  harness tools with ``runner.run_tool(...)``, then feeds function results back.
* The Gemini plugin activity publishes streaming ``reply_delta`` and model/tool request
  events while the interaction stream is drained.
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

    from temporal_agent_harness.ai_sdks.google_genai_plugin import (
        function_param,
        google_genai_client,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        OperatorCommand,
        OperatorCommandArgument,
        SlashCommand,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from ._host_driver import MontyHostDriver
    from .workflow import TASK_QUEUE


GEMINI_SUPPORTED_MODELS = ("gemini-3.5-flash", "gemini-3.1-flash-lite")
GEMINI_DEFAULT_MODEL = GEMINI_SUPPORTED_MODELS[0]
SET_MODEL_COMMAND = "set-model"
GEMINI_MODEL_OPERATOR_COMMAND = OperatorCommand(
    name="model",
    payload_name=SET_MODEL_COMMAND,
    label="/model",
    description="Set the Gemini model for this Monty session.",
    argument=OperatorCommandArgument(
        kind="enum",
        choices=GEMINI_SUPPORTED_MODELS,
        placeholder="model",
    ),
    source="agent",
)


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


@workflow.defn(name="MontyChatGeminiAgent")
@agent.defn
class MontyChatGeminiAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            operator_commands=[GEMINI_MODEL_OPERATOR_COMMAND],
            operator_command_handler=self._handle_operator_command,
        )
        self._model: str = GEMINI_DEFAULT_MODEL
        # Server-side conversation chaining id (Interactions API); updated each turn. Safe to
        # chain here because this agent uses only a function tool (no file_search).
        self._previous_interaction_id: str | None = None
        self._monty = MontyHostDriver(self._runner)
        self._monty_tool = self._build_monty_tool()

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(minutes=3),
            ),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the Gemini-backed travel assistant."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    @agent.accepts
    async def slash(self, command: SlashCommand) -> TextReply:
        """Apply a slash command to this parent agent session."""
        reply = self._handle_operator_command(command)
        if reply is not None:
            return reply
        return TextReply(
            text=(
                f"Unknown Monty slash command: `{command.name}`. Try `/model`. "
                "Harness commands include `/approvals`, `/allow-tools`, and `/status`."
            )
        )

    def _handle_operator_command(self, command: SlashCommand) -> TextReply | None:
        if command.name == SET_MODEL_COMMAND:
            return self._set_model(command.arg)
        return None

    def _set_model(self, model: str | None) -> TextReply:
        if model is None or model not in GEMINI_SUPPORTED_MODELS:
            choices = ", ".join(f"`{model}`" for model in GEMINI_SUPPORTED_MODELS)
            return TextReply(text=f"Choose one of: {choices}.")
        self._model = model
        return TextReply(text=f"Model set to **{self._model}**.")

    def _build_monty_tool(self) -> Any:
        @agent.tool_defn(inherently_safe=True)
        async def run_monty_script(script: str) -> str:
            return await self._monty.run_script(script)

        run_monty_script.__doc__ = (
            "Execute a Python `script` in the Monty sandbox and return its printed output "
            "and final value. Use this to search/book flights and hotels and build "
            "itineraries via the host functions (search_flights, search_hotels, book_flight, "
            "book_hotel, get_trip_summary). The script's LAST EXPRESSION is returned. See the "
            "system instructions for the full sandbox contract and host-function signatures."
        )
        return run_monty_script

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one Gemini conversational turn and execute any function calls it requests."""
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
        """Execute one ``run_monty_script`` call via ``run_tool`` and return its result."""
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
        """Stream one ``interactions.create`` and reduce it into actionable state."""
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
