"""Drive the Monty travel agent from a Temporal workflow, purely through the AgentService Nexus contract.

``NexusTravelDemoWorkflow`` is the caller: it has no harness client and doesn't import the agent.
Over two turns it sends messages, follows the agent's event stream with ``pollMessages``,
approves every gated tool call, answers the agent's ``ask_user`` questions, and sends messages
mid-turn: a steering message (Monty's ``ask`` joins the open turn and steers) and a ``set_model``
call (an ``accept`` handler that runs alongside the turn). It finishes with ``queryAgentStatus``,
``queryAgentInterface`` and ``closeSession``.

Run it with ``bazel run //python/examples/nexus_travel_demo:caller`` once the agent worker, the
adapter worker and the Nexus endpoint are up (see README.md). Every step is logged here as it
happens; the caller's workflow history in the Temporal UI shows each Nexus operation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    import base64
    import json

    from temporalio.api.common.v1 import Payload

    from temporal_agent_harness.nexus_agent_adapter.generated import (
        AgentService,
        ApproveToolCallInput,
        PollMessagesInput,
        ProvideCallbackResultInput,
        ProvideCallbackResultInputResult,
        QuerySessionInput,
        SendAgentMessageInput,
        SendAgentMessageInputPayload,
        SendMessageOutput,
    )

CALLER_TASK_QUEUE = "nexus-travel-demo-caller"
DEFAULT_ENDPOINT = "nexus-travel-agent"

_SHORT = timedelta(minutes=1)
# pollMessages completes when the agent publishes; a model call can take a while.
_POLL = timedelta(minutes=5)
# Each pollMessages leaves a completion callback on the agent's workflow, and the server allows
# 32 per workflow by default. Waiting between polls lets each one collect everything published
# meanwhile, instead of returning with the first event.
_POLL_SPACING = timedelta(seconds=5)
_CALLBACK_LIMIT = 32


@dataclass
class Turn:
    """One message, and what to send mid-turn once the agent first requests a tool."""

    text: str
    steer: str | None = None
    set_model: str | None = None


@dataclass
class DemoInput:
    endpoint: str
    session_id: str
    turns: list[Turn] = field(default_factory=list)
    # False leaves Monty's approval policy alone, so the caller approves each gated call.
    skip_approvals: bool = True


TURNS = [
    Turn(
        text=(
            "Hi! I'm planning a trip from San Francisco (SFO) to Tokyo (HND), leaving "
            "March 10 and coming back March 17, for one traveler. Can you find flight options?"
        ),
        steer="Only nonstop flights, please.",
        set_model="gemini-3.8-flash",
    ),
    Turn(
        text="Book the cheapest nonstop round trip for traveler Ada Lovelace.",
        steer="Also note on the trip board that Ada wants a window seat.",
    ),
]


def _decode(data: str) -> dict:
    """A stream item's data: base64 of a serialized Payload whose data is the event's JSON."""
    return json.loads(Payload.FromString(base64.b64decode(data)).data)


def _answer(question: str, options: list[str] | None) -> str:
    """A canned answer for the agent's ``ask_user`` questions."""
    if options:
        return options[0]
    lowered = question.lower()
    if "name" in lowered:
        return "Ada Lovelace"
    if "budget" in lowered or "price" in lowered:
        return "Keep it reasonable, under $1500 for flights."
    return "Whatever you think is best."


@workflow.defn
class NexusTravelDemoWorkflow:
    @workflow.run
    async def run(self, input: DemoInput) -> list[str]:
        self._input = input
        self._log: list[str] = []
        self._polls = 0
        self._policy_set = False
        self._client = workflow.create_nexus_client(service=AgentService, endpoint=input.endpoint)

        for number, turn in enumerate(input.turns, start=1):
            self._say(f"=== Turn {number} ===")
            await self._run_turn(turn)
            self._say(
                f"[pollMessages] {self._polls} polls so far, of the agent workflow's "
                f"{_CALLBACK_LIMIT} callbacks"
            )

        status = await self._client.execute_operation(
            AgentService.query_agent_status,
            QuerySessionInput(session_id=input.session_id),
            schedule_to_close_timeout=_SHORT,
        )
        self._say(
            f"[status] turn {status.current_turn}, turn active: {status.turn_active}, "
            f"pending approvals: {len(status.pending_approvals)}, "
            f"approval policy auto-approves: {status.approval_policy.auto_approve_tools}"
        )
        interface = await self._client.execute_operation(
            AgentService.query_agent_interface,
            QuerySessionInput(session_id=input.session_id),
            schedule_to_close_timeout=_SHORT,
        )
        self._say(
            "[interface] "
            + ", ".join(f"{h.name} (mid-turn: {h.mid_turn})" for h in interface.handlers)
        )
        closed = await self._client.execute_operation(
            AgentService.close_session,
            QuerySessionInput(session_id=input.session_id),
            schedule_to_close_timeout=_SHORT,
        )
        self._say(f"[closeSession] already closed: {closed.already_closed}")
        return self._log

    def _say(self, line: str) -> None:
        self._log.append(line)
        workflow.logger.info(line)

    async def _send(self, msg_type: str, payload: dict) -> SendMessageOutput:
        out = await self._client.execute_operation(
            AgentService.send_agent_message,
            SendAgentMessageInput(
                session_id=self._input.session_id,
                msg_type=msg_type,
                payload=SendAgentMessageInputPayload(additional_properties=payload),
            ),
            schedule_to_close_timeout=_SHORT,
        )
        self._say(
            f"[sendAgentMessage {msg_type}] {json.dumps(payload)} -> turn {out.turn_number}, "
            f"{out.disposition}, message {out.message_id}"
        )
        return out

    async def _run_turn(self, turn: Turn) -> None:
        sent = await self._send("ask", {"text": turn.text})
        if self._input.skip_approvals and not self._policy_set:
            # Mid-turn, before the model's first tool call: Monty's set_approval_policy joins the
            # open turn, so nothing this session runs waits on a human. The policy outlives the
            # turn. approveToolCall below still answers anything already gated.
            self._policy_set = True
            await self._send("set_approval_policy", {"new_policy": "dangerously_skip_all"})
        cursor = sent.stream_head_offset
        mid_turn_sent = False
        reply: list[str] = []
        while True:
            await workflow.sleep(_POLL_SPACING)
            self._polls += 1
            out = await self._client.execute_operation(
                AgentService.poll_messages,
                PollMessagesInput(session_id=self._input.session_id, cursor=cursor),
                schedule_to_close_timeout=_POLL,
            )
            if out.closed:
                self._say("[pollMessages] the session has closed")
                return
            cursor = out.next_offset
            for item in out.items:
                envelope = _decode(item.data)
                event = envelope["event"]
                kind = event["type"]
                own = envelope.get("message_id") == sent.message_id

                if kind == "reply_delta" and own:
                    reply.append(event["text"])
                elif kind == "tool_requested":
                    self._say(f"[tool requested] {event['tool_name']}")
                    if not mid_turn_sent:
                        mid_turn_sent = True
                        if turn.steer:
                            await self._send("ask", {"text": turn.steer})
                        if turn.set_model:
                            await self._send("set_model", {"model": turn.set_model})
                elif kind == "tool_approval_requested":
                    await self._client.execute_operation(
                        AgentService.approve_tool_call,
                        ApproveToolCallInput(
                            session_id=self._input.session_id,
                            tool_id=event["tool_id"],
                            approved=True,
                        ),
                        schedule_to_close_timeout=_SHORT,
                    )
                    self._say(f"[approveToolCall] approved {event['tool_name']}")
                elif kind == "callback_requested":
                    question = event["tool_input"].get("question", "")
                    answer = _answer(question, event["tool_input"].get("options"))
                    await self._client.execute_operation(
                        AgentService.provide_callback_result,
                        ProvideCallbackResultInput(
                            session_id=self._input.session_id,
                            tool_id=event["tool_id"],
                            result=ProvideCallbackResultInputResult(
                                additional_properties={"value": answer}
                            ),
                        ),
                        schedule_to_close_timeout=_SHORT,
                    )
                    self._say(f"[provideCallbackResult] Q: {question!r} A: {answer!r}")
                elif kind in ("steering_enqueued", "steering_claimed", "steering_dropped"):
                    self._say(f"[{kind}] {json.dumps({k: v for k, v in event.items() if k != 'type'})}")
                elif kind == "message_handler_end" and not own:
                    self._say(f"[mid-turn reply] {json.dumps(event.get('output'))}")
                elif kind == "message_handler_end" and own:
                    self._say("[agent] " + ("".join(reply) or json.dumps(event.get("output"))))
                    return
                elif kind == "message_handler_error" and own:
                    self._say(f"[agent error] {event.get('message')}")
                    return


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    workflow.logger.workflow_info_on_message = False
    endpoint = os.environ.get("NEXUS_ENDPOINT", DEFAULT_ENDPOINT)
    session_id = uuid.uuid4().hex[:8]
    client = await Client.connect(**ClientConfig.load_client_connect_config())
    async with Worker(client, task_queue=CALLER_TASK_QUEUE, workflows=[NexusTravelDemoWorkflow]):
        workflow_id = f"nexus-travel-caller-{session_id}"
        print(f"Caller workflow {workflow_id}, agent session {session_id!r}, endpoint {endpoint!r}")
        log = await client.execute_workflow(
            NexusTravelDemoWorkflow.run,
            DemoInput(
                endpoint=endpoint,
                session_id=session_id,
                turns=TURNS,
                skip_approvals=os.environ.get("NEXUS_DEMO_APPROVE_EACH_TOOL") != "1",
            ),
            id=workflow_id,
            task_queue=CALLER_TASK_QUEUE,
        )
    print("\n--- Transcript ---")
    print("\n".join(log))


if __name__ == "__main__":
    asyncio.run(main())
