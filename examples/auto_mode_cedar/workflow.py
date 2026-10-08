"""AUTO MODE, judged by a Cedar policy: the Monty travel agent under ``cedar_evaluator``.

The same conversational Code Mode agent as ``examples/auto_mode``, with the same tools and trip
board, but each gated call is put to a Cedar tool policy served over Nexus (the
``VerifyToolCalls`` operation of the temporal-untrusted-workers Cedar verifier) instead of to
Jev. The rules are ``policies/travel.cedar``; nothing in this file says which tool may do what.

Two kinds of call reach the policy:

  * ``run_travel_code``, the script. Cedar can't read a script, so the evaluator sends what
    ``analyze_script`` reads off it (which host functions it calls, how often, in loops or
    concurrently) and what ``jev_script_classifier`` answers about it (for instance, whether it
    could run away). The policy permits scripts and forbids, say, booking in a loop.
  * Each host call the script makes, with its concrete arguments. The policy permits searches,
    refuses a booking with no name on it, and permits no other booking, so every booking comes
    to you.

Needs a ``GEMINI_API_KEY`` (the conversation) and a ``TYPESAFE_API_KEY`` (the classifier), and
the Cedar verifier running behind the ``tool-verifier`` Nexus endpoint; see the README.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from google.genai._interactions.types import FunctionCallStep
    from google.genai._interactions.types.function_result_step_param import (
        FunctionResultStepParam,
    )
    from google.genai.client import AsyncClient

    from temporal_agent_harness.ai_sdks.google_genai_plugin import (
        InteractionConversation,
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

    from ..auto_mode.workflow import (
        CODE_MODE_TOOL_NAME,
        DEFAULT_MODEL,
        SYSTEM_INSTRUCTION,
        PolicyUpdate,
        SetModel,
    )
    from ..monty import activities, trip_board


TASK_QUEUE = "auto-mode-cedar-agent"
# Tool calls reach the policy as "travel-agent/<tool>". Fixed rather than the task-queue default,
# so the policy doesn't change when the task queue does.
ACTION_PREFIX = "travel-agent"
# The Nexus endpoint the Cedar verifier serves; `just endpoint` creates it.
POLICY_ENDPOINT = "tool-verifier"

# The tools the script can call. The board tools ride along so a script can record a booking
# in the same breath as making it.
SCRIPT_TOOLS = [*activities.ALL_TOOLS, *trip_board.BOARD_TOOLS]

# One catch-all set. Auto mode only consults an evaluator for tools some criteria set covers;
# with Cedar deciding, the set needs no rules of its own.
_AUTO_APPROVAL_CRITERIA = agent.AutoApprovalCriteria(
    sets={"cedar": agent.AutoApprovalCriteriaSet(effect="Judged by the Cedar tool policy.")},
    default="cedar",
)


def build_code_tool(board: object | None = None):
    """The Code Mode tool, built the same way here and by ``schema.py``."""
    return agent.code_mode_tool(
        SCRIPT_TOOLS,
        name=CODE_MODE_TOOL_NAME,
        injections={"board": board} if board is not None else None,
    )


@agent.defn(name="AutoModeCedarTravelAgent")
class AutoModeCedarTravelAgentWorkflow:
    trip_board = agent.state(trip_board.TripBoard)

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # The board tools write to the agent's own notes, so they are approved above auto
            # mode and never cost a policy call.
            approval_policy_default=ToolApprovalPolicy.auto_mode(
                pre_approved_tools=trip_board.BOARD_TOOL_NAMES
            ),
            auto_approval_criteria_default=_AUTO_APPROVAL_CRITERIA,
            auto_mode_evaluator=agent.cedar_evaluator(
                endpoint=POLICY_ENDPOINT,
                action_prefix=ACTION_PREFIX,
                script_classifier=agent.jev_script_classifier(),
            ),
        )
        self._model: str = DEFAULT_MODEL
        self._conversation = InteractionConversation()
        self._code_tool = build_code_tool(self.trip_board)
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=3)),
            runner=self._runner,
        )

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the travel assistant. Describe the trip you want (flights, hotels,
        dates, traveler name) in plain text; the assistant converses, writes and runs Python
        scripts against a simulated travel backend as needed, and replies with the results."""
        return TextReply(text=await self._handle_chat_turn(self._gemini, message.text))

    @agent.accepts(mid_turn=MidTurn.ACCEPT, model_callable=False)
    async def set_model(self, message: SetModel) -> TextReply:
        """Set the model this session uses for subsequent turns."""
        self._model = message.model
        return TextReply(text=f"Model set to **{message.model}**.")

    @agent.accepts(mid_turn=MidTurn.ACCEPT, model_callable=False)
    async def set_approval_policy(self, policy_update: PolicyUpdate) -> TextReply:
        """Set the agent's approval posture. `auto_mode` asks the Cedar policy about each
        gated call; `human_approval_only` sends every one to you."""
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
        self._runner.set_approval_policy(updated_policy)
        return TextReply(text=f"Approval posture is now **{policy_update.posture}**.")

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: stream the model, dispatch any ``run_travel_code``
        calls, feed results back, and loop until the model replies with no further calls."""
        tools = [function_param(self._code_tool)]
        self._conversation.add_user_text(user_text)
        while True:
            stream = await gemini.interactions.create(
                model=self._model,
                input=self._conversation.steps,
                system_instruction=SYSTEM_INSTRUCTION,
                tools=tools,
                stream=True,
            )
            reply = await self._conversation.read_reply(stream)
            if not reply.function_calls:
                return reply.text
            self._conversation.add_function_results(
                await asyncio.gather(*(self._run_one_tool(fc) for fc in reply.function_calls))
            )

    async def _run_one_tool(self, call: FunctionCallStep) -> FunctionResultStepParam:
        """Execute one ``run_travel_code`` call via ``run_tool`` and return its result."""
        try:
            if call.name != self._code_tool.__name__:
                raise ValueError(f"unknown tool: {call.name!r}")
            result = str(
                await self._runner.run_tool(call.id, self._code_tool, injections=None, **call.arguments)
            )
            is_error = False
        except Exception as e:
            result, is_error = str(e), True
        response: FunctionResultStepParam = {
            "type": "function_result",
            "call_id": call.id,
            "name": call.name,
            "result": result,
        }
        if is_error:
            response["is_error"] = True
        if call.signature:
            response["signature"] = call.signature
        return response
