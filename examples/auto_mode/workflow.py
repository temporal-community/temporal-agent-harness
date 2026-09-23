"""AUTO MODE: the Monty travel agent with its gated tool calls judged by Jev.

The same conversational Code Mode agent as ``examples/monty`` — same tools, same trip board —
but running under ``ToolApprovalPolicy.auto_mode(...)``, so each gated call is first put to
``agent.jev_evaluator()`` against the rulebook in :data:`_AUTO_APPROVAL_CRITERIA`. Jev may
approve it, deny it, or leave it to you; anything it does not approve comes to you exactly as
it would in the Monty example. Needs a ``GEMINI_API_KEY`` (the conversation) AND a
``TYPESAFE_API_KEY`` (the evaluator) — see ``worker.py``.

It reuses Monty's travel tools UNCHANGED: they declare no criteria set of their own, so the
rulebook assigns each one by NAME in ``AutoApprovalCriteria.tools``. That is the same mechanism
that brings tools you did not write (an MCP server's, say) under auto mode.

The user chats in plain text; a *model in the loop* converses to gather what it needs, then
**writes a Python script and runs it** to search and book flights/hotels, and replies in prose.
It does this through the harness Code Mode feature: :func:`agent.code_mode_tool` turns the travel
activity tools into a single ``run_travel_code`` tool that executes a model-authored script in a
sandbox, where each tool is an async host function the script calls. Every host call runs as a
durable, approval-gated activity, and the script can combine many with real control flow (loops,
``asyncio.gather`` concurrency).

The conversational front end is a Gemini Interactions tool-calling loop exposing that one Code
Mode tool. It uses only a custom *function* tool, which chains cleanly across turns via
``previous_interaction_id`` — so multi-turn conversation works. The Code Mode tool advertises the
exact host-function signatures + result shapes in its own (generated) description, so the system
prompt only needs to set the persona and point the model at the tool.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from functools import partial
from typing import Literal, Sequence

from pydantic import BaseModel
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
        MidTurn,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from ..monty import activities, trip_board


# Its own queue, not Monty's: every worker on a queue must register the same workflow types.
TASK_QUEUE = "auto-mode-agent"
SUPPORTED_MODELS = ("gemini-3.8-flash", "gemini-3.1-flash-lite")
DEFAULT_MODEL = SUPPORTED_MODELS[0]


class SetModel(BaseModel):
    """Which model this session should use for subsequent turns."""

    # A Literal (not a bare str) so the choice is enforced rather than merely suggested:
    # pydantic rejects anything else at the update boundary, and the same constraint shows up
    # as an enum in this handler's `parameters` JSON schema — which is what lets a generic
    # client render a dropdown without knowing anything about Monty.
    model: Literal[SUPPORTED_MODELS]  # type: ignore[valid-type]


class PolicyUpdate(BaseModel):
    """The agent's whole approval posture. One field, because these are alternatives.

    Auto mode is a way of ALLOW-LISTING — a dynamic one that reads a call's arguments rather
    than only its name — so it is a peer of the static allow-lists, not an extra switch beside
    them. Modelling it as one choice is what makes ``human_approval_only`` and ``auto_mode``
    impossible to ask for together: "nothing is allow-listed" and "calls are allow-listed by
    an evaluator" are opposite answers to the same question, and a separate boolean switch
    could only ever contradict whichever posture had just been named.

    Whatever a posture does not allow-list goes to a human — including every call auto mode
    declines to approve, so turning it on reduces what you are asked about without ever taking
    the last word on an unclear call away from you.
    """

    posture: Literal[
        "human_approval_only", "allow_safe", "auto_mode", "dangerously_skip_all"
    ]


# The auto-mode rulebook. Configuration, not code: the sets say what KIND of call a
# "books_travel" or "read_only" tool makes, and `tools` points each of Monty's travel tools at
# one BY NAME — they carry no `auto_approval_criteria=` of their own — so a deployment can
# retune it, or a user can redefine it mid-session via `runner.set_auto_approval_criteria`,
# without the tools changing. Anything unassigned falls to the `cautious` catch-all.
CODE_MODE_TOOL_NAME = "run_travel_code"
_AUTO_APPROVAL_CRITERIA = agent.AutoApprovalCriteria(
    sets={
        "read_only": agent.AutoApprovalCriteriaSet(
            effect="Looks up flight, hotel or trip data and returns it. Books nothing.",
            approve_when=("the call only searches or summarizes",),
            escalate_when=("the arguments name a traveller the user never mentioned",),
        ),
        "books_travel": agent.AutoApprovalCriteriaSet(
            effect=(
                "Reserves a real flight or hotel in a traveller's name. Cannot be undone "
                "from inside this system once the provider confirms it."
            ),
            deny_when=("the traveller is not the person the user asked to book for",),
            escalate_when=("anything is actually being booked",),
        ),
        "cautious": agent.AutoApprovalCriteriaSet(
            effect="Unknown — nobody has written rules for this tool.",
            escalate_when=("always; a person decides what has not been described",),
        ),
        "run_travel_code": agent.AutoApprovalCriteriaSet(
            effect=(
                "Executes tools programmatically in an isolated sandbox. All tools "
                "executed within this script receive their own gating, so gating this "
                "tool is never a security decision."
            ),
            approve_when=("The script executes a bounded, well-scoped series of operations.",),
            escalate_when=("The script will run an unbounded or very large number of actions.",),
            deny_when=("The script is guaranteed to be an invalid program.",),
            min_confidence=0.5,
        ),
    },
    default="cautious",
    tools={
        CODE_MODE_TOOL_NAME: "run_travel_code",
        "search_flights": "read_only",
        "search_hotels": "read_only",
        "get_trip_summary": "read_only",
        "book_flight": "books_travel",
        "book_hotel": "books_travel",
    },
)


SYSTEM_INSTRUCTION = """\
You are a friendly travel-booking assistant. You help users search and book flights and \
hotels and assemble trip itineraries. You don't have these abilities directly — instead you \
write small async Python scripts and run them with the `run_travel_code` tool, which exposes the \
travel operations as async host functions your script calls. The tool's description gives the \
exact host-function signatures and result shapes — follow them; index results with normal Python.

How to behave:
- Converse naturally. Ask brief clarifying questions when you're missing something essential \
(origin/destination, dates, traveler name) — don't interrogate; make reasonable assumptions \
and state them.
- When you have enough to make progress, WRITE A SCRIPT and call `run_travel_code`. Run \
independent host calls concurrently with `asyncio.gather`; keep each script focused (search, or \
book, or summarize) so you can react to results.
- After a tool result, read it and reply to the user in plain, friendly prose — summarize \
options, prices, confirmations. You may run more scripts in follow-up turns as the \
conversation continues.
- Never invent flight/hotel ids or confirmation codes — only use ones returned by a script.

Keep the trip board current. The same script can call the board host functions — \
`open_trip`, `record_booking`, `complete_trip_tasks`, `add_trip_tasks`, `set_trip_status`, \
`read_trip_board` — and they are how the user watches you work. The board is not a summary you \
write at the end; it is the running state of the trip, so:
- `open_trip` as soon as you know which trip this is, BEFORE you search anything. Keep the \
`trip_id` it returns and use it for every later call (or pass "latest").
- In the SAME script that books something, `record_booking` it with the confirmation code the \
booking returned, and name the checklist items it finishes in `completes`. Never let the board \
show a booked flight next to an outstanding "book the flight".
- `complete_trip_tasks` as soon as a task is genuinely done, and `add_trip_tasks` the moment you \
find the trip needs something the plan did not have.
- `set_trip_status` to "booked" once nothing is left, or "cancelled" if the user calls it off.
- `read_trip_board` at the start of a follow-up request to recall where you left off.
Board calls are cheap and are not gated on the user's approval — there is nothing to approve \
about your own notes — so there is never a reason to batch them up or skip them."""


@workflow.defn(name="AutoModeTravelAgent")
@agent.defn
class AutoModeTravelAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Demo stance: every tool call that reaches the outside world is gated — the
            # `run_travel_code` tool and each travel host call the script makes — and put to
            # the evaluator first. The trip-board tools are pre-approved by name, ABOVE auto
            # mode: they write to the agent's own notes, so there is nothing to judge, and
            # sending them to Jev would spend a model call per board update for nothing.
            approval_policy_default=ToolApprovalPolicy.auto_mode(
                pre_approved_tools=trip_board.BOARD_TOOL_NAMES
            ),
            # AUTO MODE, on. The switch lives on the policy, so the same agent runs with a
            # human on every gated call by simply not enabling it — and a caller can impose
            # either posture per session via `AgentConfig.approval_policy`.
            auto_approval_criteria_default=_AUTO_APPROVAL_CRITERIA,
            auto_mode_evaluator=agent.jev_evaluator(),
        )
        self._model: str = DEFAULT_MODEL
        # Server-side conversation chaining id (Interactions API); updated each turn. Safe to
        # chain here because this agent uses only a function tool (no file_search).
        self._previous_interaction_id: str | None = None
        # The single model-facing tool: Code Mode over the travel tools. The model writes a
        # Python script that calls the travel operations as async host functions; each host call
        # runs as a durable, approval-gated activity via run_tool.
        # THE OPT-IN, and the whole of it: one call, and from here every committed
        # `mutate()` on this ref is published to the agent's turn_events stream as JSON
        # Patch ops. Nothing below ever mentions an event, a topic, or publishing.
        self._board = self._runner.state("trip_board", trip_board.TripBoard())
        self._code_tool = agent.code_mode_tool(
            # The travel tools plus the board tools, in one sandbox: a script can book a
            # flight and record it on the board without a round trip through the model, so
            # the board cannot drift from what was actually booked.
            [*activities.ALL_TOOLS, *trip_board.BOARD_TOOLS],
            name=CODE_MODE_TOOL_NAME,
            # Hidden from the model and from the generated stubs: the script names the
            # trip, never the state it lives in.
            injections={"board": self._board},
        )

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

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the travel assistant. Describe the trip you want (flights, hotels,
        dates, traveler name) in plain text; the assistant converses, writes and runs Python
        scripts against a simulated travel backend as needed, and replies with the results."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    # ACCEPT: reconfiguring the session is not work, so it should not wait behind work. It
    # joins the open turn and applies immediately, which is the whole point of being able to
    # switch models while the agent is mid-conversation. model_callable=False keeps it off a
    # parent agent's generated toolset by default — choosing the model is an operator's call,
    # not something a driving model should do to its own child.
    @agent.accepts(mid_turn=MidTurn.ACCEPT, model_callable=False)
    async def set_model(self, message: SetModel) -> TextReply:
        """Set the model this session uses for subsequent turns. Takes effect on the next
        model call, so a turn already in flight finishes on the model it started with."""
        self._model = message.model
        return TextReply(text=f"Model set to **{message.model}**.")

    @agent.accepts(mid_turn=MidTurn.ACCEPT, model_callable=False)
    async def set_approval_policy(self, policy_update: PolicyUpdate) -> TextReply:
        """Set the agent's approval posture: what gets allow-listed, and therefore what still
        goes to a human.

        `human_approval_only` allow-lists nothing — every call waits for you, and an auto mode
        evaluator is not consulted even though one is wired. `allow_safe` allow-lists tools
        that declared themselves inherently safe. `auto_mode` hands each remaining call to the
        Jev evaluator, which may allow it, deny it, or leave it to you anyway.
        `dangerously_skip_all` gates nothing at all.

        A posture REPLACES the policy, allow-list included — including anything an "approve and
        stop asking" added this session. That is the point of it being a posture rather than a
        patch: you get exactly what you asked for and nothing left over."""
        match policy_update.posture:
            case "human_approval_only":
                updated_policy = ToolApprovalPolicy.always_require_human_approval()
            case "allow_safe":
                updated_policy = ToolApprovalPolicy.allow_inherently_safe()
            case "auto_mode":
                updated_policy = ToolApprovalPolicy.auto_mode(
                    pre_approved_tools=trip_board.BOARD_TOOL_NAMES
                )
            case "dangerously_skip_all":
                updated_policy = ToolApprovalPolicy.dangerously_skip_all()
            case _:
                return TextReply(text=f"Unknown approval posture requested: {policy_update}")
        self._runner.set_approval_policy(updated_policy)

        allow_listed = ", ".join(f"`{n}`" for n in sorted(updated_policy.auto_approve_tools))
        if updated_policy.auto_approve_inherently_safe:
            allow_listed = f"{allow_listed + ', ' if allow_listed else ''}any inherently-safe tool"
        if updated_policy.auto_mode_enabled:
            allow_listed = (
                f"{allow_listed + ', plus ' if allow_listed else ''}"
                "whatever `jev_evaluator` approves against its tool's criteria"
            )
        return TextReply(
            text=(
                f"Approval posture is now **{policy_update.posture}**.\n\n"
                f"- Allow-listed: {allow_listed or '_nothing_'}\n"
                + (
                    "- Nothing is gated at all, so nothing reaches you."
                    if updated_policy.dangerously_skip_all_approvals
                    else "- Everything else: comes to you for approval."
                )
            )
        )

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: stream the model, dispatch any ``run_travel_code``
        calls, feed results back, and loop until the model replies with no further calls.

        Updates ``self._previous_interaction_id`` for chaining the next turn (no file_search
        here, so chaining is safe)."""
        tools = [function_param(self._code_tool)]
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

            next_input = await asyncio.gather(*(self._run_one_tool(fc) for fc in pending_calls))

    async def _run_one_tool(self, call: FunctionCallStep) -> FunctionResultStepParam:
        """Execute one ``run_travel_code`` call via ``run_tool`` and return its result.

        ``run_tool`` parks the call id so the script's host calls and the tool's own
        lifecycle events correlate with the streaming activity's ``tool_requested``."""
        try:
            if call.name != self._code_tool.__name__:
                raise ValueError(f"unknown tool: {call.name!r}")
            result = await self._runner.run_tool(call.id, self._code_tool, **call.arguments)
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
        tools: Sequence[ToolParam],
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
            stream = await interactions_create_fn(previous_interaction_id=previous_interaction_id)
        else:
            stream = await interactions_create_fn()

        text_parts: list[str] = []
        calls_by_index: dict[int, FunctionCallStep] = {}
        arg_buffers: dict[int, str] = {}
        interaction_id: str | None = None
        async for event in stream:
            match event:
                case ErrorEvent(error=Error(message=msg, code=code)):
                    raise ApplicationError(msg or "stream error", type=code or "stream_error")
                case ErrorEvent():
                    raise ApplicationError("unknown stream error", type="stream_error")
                case StepStart(index=idx, step=FunctionCallStep() as call):
                    calls_by_index[idx] = call
                case StepDelta(index=idx, delta=DeltaArgumentsDelta(arguments=args)) if args:
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
            calls_by_index[idx].model_copy(update={"arguments": json.loads(arg_buffers[idx])})
            if arg_buffers.get(idx)
            else calls_by_index[idx]
            for idx in sorted(calls_by_index)
        ]
        return "".join(text_parts), function_calls, interaction_id
