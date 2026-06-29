"""Conversational Monty agent: chat in text, and the model writes + runs its own scripts.

This is a demo-oriented twist on :class:`MontyDynamicAgentWorkflow`. That agent receives a
pre-written script per turn and runs it. This one puts a *model in the loop*: the user
chats in plain text, the model converses to gather what it needs, and when it decides it's
ready it **writes its own Python script and calls the ``run_monty_script`` tool** to execute
it in the Monty sandbox. The model then reads the script's output and replies in prose.

The Monty side is IDENTICAL to :class:`MontyDynamicAgentWorkflow`: the same async batch
driver (``monty_start_batch`` → ``monty_resume_batch``) backed by the same durable travel
activities (``_dispatch_host_call`` / ``_run_activity_tool``). The only new part is the
conversational front end — an OpenAI Agents SDK tool-calling loop exposing a single tool,
``run_monty_script``.

The OpenAI Agents SDK receives the conversation state from the workflow each turn and the
Temporal OpenAI Agents plugin routes model calls through activities, keeping credentials out
of workflow code.
"""

from __future__ import annotations

from typing import Any

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import ModelResponse, RunContextWrapper, Runner, TResponseInputItem
    from agents.lifecycle import RunHooksBase

    from temporal_agent_harness.ai_sdks.openai_agents_plugin import as_openai_agent_tool
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        ModelInteractionEnded,
        ModelInteractionStarted,
        OperatorCommand,
        OperatorCommandArgument,
        SlashCommand,
        TextMessage,
        TextReply,
        TokenUsage,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from ._host_driver import MontyHostDriver


TASK_QUEUE = "monty-dynamic-agent"
SUPPORTED_MODELS = ("gpt-5.4-mini", "gpt-5.4")
DEFAULT_MODEL = SUPPORTED_MODELS[0]
SET_MODEL_COMMAND = "set-model"
MODEL_OPERATOR_COMMAND = OperatorCommand(
    name="model",
    payload_name=SET_MODEL_COMMAND,
    label="/model",
    description="Set the model for this Monty session.",
    argument=OperatorCommandArgument(
        kind="enum",
        choices=SUPPORTED_MODELS,
        placeholder="model",
    ),
    source="agent",
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


def _reported_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _token_usage_from_response(response: ModelResponse) -> TokenUsage:
    usage = response.usage
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return TokenUsage(
        input_tokens=_reported_int(getattr(usage, "input_tokens", None)),
        output_tokens=_reported_int(getattr(usage, "output_tokens", None)),
        thought_tokens=_reported_int(getattr(output_details, "reasoning_tokens", None)),
        cached_tokens=_reported_int(getattr(input_details, "cached_tokens", None)),
        total_tokens=_reported_int(getattr(usage, "total_tokens", None)),
    )


class _HarnessOpenAIRunHooks(RunHooksBase[Any, Any]):
    def __init__(self, runner: AgentWorkflowRunner, model: str) -> None:
        self._runner = runner
        self._model = model

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: OpenAIAgent[Any],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        self._runner.publish(ModelInteractionStarted(model=self._model))

    async def on_llm_end(
        self,
        context: RunContextWrapper[Any],
        agent: OpenAIAgent[Any],
        response: ModelResponse,
    ) -> None:
        self._runner.publish(
            ModelInteractionEnded(
                model=self._model,
                usage=_token_usage_from_response(response),
            )
        )


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
            operator_commands=[MODEL_OPERATOR_COMMAND],
            operator_command_handler=self._handle_operator_command,
        )
        self._model: str = DEFAULT_MODEL
        self._conversation: list[TResponseInputItem] = []
        # Shared execution half: runs the model-authored script via the async batch loop
        # (composition — same driver the script-only MontyDynamicAgent uses).
        self._monty = MontyHostDriver(self._runner)
        # The single model-facing tool: an inline workflow tool that runs a model-authored
        # script through the Monty async batch driver. Built once, closing over `self`.
        self._monty_tool = self._build_monty_tool()

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the travel assistant. Describe the trip you want (flights, hotels,
        dates, traveler name) in plain text; the assistant converses, writes and runs Python
        scripts against a simulated travel backend as needed, and replies with the results."""
        reply_text = await self._handle_chat_turn(message.text)
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
        if model is None or model not in SUPPORTED_MODELS:
            choices = ", ".join(f"`{model}`" for model in SUPPORTED_MODELS)
            return TextReply(text=f"Choose one of: {choices}.")
        self._model = model
        return TextReply(text=f"Model set to **{self._model}**.")

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

    async def _handle_chat_turn(self, user_text: str) -> str:
        """Run one conversational turn with the OpenAI Agents SDK."""
        sdk_agent = OpenAIAgent(
            name="Monty",
            instructions=SYSTEM_INSTRUCTION,
            model=self._model,
            tools=[as_openai_agent_tool(self._runner, self._monty_tool)],
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": user_text},
        ]
        result = await Runner.run(
            sdk_agent,
            input=input_items,
            hooks=_HarnessOpenAIRunHooks(self._runner, self._model),
        )
        self._conversation = result.to_input_list()
        return str(result.final_output)

    # Monty execution (the async batch loop + host-call dispatch) lives in the shared
    # MontyHostDriver held in self._monty (composition); call self._monty.run_script(...).
