# ABOUTME: The harness's Claude parts end to end: the claude-agent-sdk wheel's bundled Claude Code,
# driven from a harness agent through the upstream plugin's transport, against a scripted Anthropic
# API on localhost. Asserts each tool call's harness events under Claude's own tool_use id, that the
# harness approval gate decides every call, even one Claude Code would run unasked, that a
# subagent's calls, Claude's thinking, and harness tools (a callback tool included) reach the turn
# stream, that the next turn resumes the session, that a turn finishes after a Worker restart or
# Claude Code dying without asking again, with a cut-off subagent carrying on as itself, that a custom connector and the transport's options are used, and that each
# history replays.
#
# No API key is needed: Claude Code talks to the scripted API, with a config dir and working
# directory under tmp_path.
#
# Run with: uv run pytest tests/ai_sdks/claude_agent_sdk_harness/test_claude_session.py -v

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import uuid
from collections.abc import AsyncIterator, Container, Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from temporalio.claude_agent_sdk import (
    ClaudeAgentSDKPlugin,
    ClaudeCodeProcess,
    ConnectActivityInput,
    SubprocessConnector,
)
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from temporal_agent_harness.ai_sdks import claude_agent_sdk_harness
from temporal_agent_harness.ai_sdks.claude_agent_sdk_harness import HarnessClaudeSession
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TOOL_APPROVAL_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    ToolApprovalDecision,
    ToolApprovalResult,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext
from temporal_agent_harness.plugin import AgentHarnessPlugin

from tests.ai_sdks.claude_agent_sdk_harness._claude_workflow import (
    CLAUDE_STREAM_TOPIC,
    CONNECT_TIMEOUT,
    ClaudeSessionTestAgent,
)

E = AgentEventType


class ScriptedApi:
    """The Anthropic Messages API, answering each request from a script.

    A request gets the answer of the first key its last user message contains; a tool result reads
    as ``tool_result:<tool_use_id>``. An answer is a text, a tool use or thinking block as a dict,
    a list of them, or a function of the request's body that returns one. Each request's body goes
    in ``requests``.
    """

    def __init__(self) -> None:
        self.answers: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append(body)
                prompt = _prompt_of(body)
                answer = next(
                    (a for key, a in api.answers.items() if key in prompt), ""
                )
                if callable(answer):
                    answer = answer(body)
                data = _stream(answer, body.get("model", "claude"))
                with contextlib.suppress(
                    OSError
                ):  # an interrupted Claude Code stops reading
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        return Handler


def _prompt_of(body: dict[str, Any]) -> str:
    """A request's last user message, with each tool result as ``tool_result:<tool_use_id>``."""
    content = next(
        m["content"] for m in reversed(body["messages"]) if m["role"] == "user"
    )
    if isinstance(content, str):
        return content
    return " ".join(
        b.get("text") or f"tool_result:{b.get('tool_use_id')}" for b in content
    )


def _stream(answer: Any, model: str) -> bytes:
    """The Messages API's event stream for a text, tool use, or thinking block, or a list."""
    blocks = answer if isinstance(answer, list) else [answer]
    message = {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "model": model,
        "role": "assistant",
        "content": [],
        "usage": {
            "input_tokens": 3,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 7,
        },
    }
    events: list[dict[str, Any]] = [{"type": "message_start", "message": message}]
    for index, block in enumerate(blocks):
        if isinstance(block, str):
            start = {"type": "text", "text": ""}
            # A word per delta, as a model streams it.
            deltas = [
                {"type": "text_delta", "text": word}
                for word in re.findall(r"\S+\s*", block)
            ]
        elif block["type"] == "thinking":
            start = {"type": "thinking", "thinking": ""}
            deltas = [{"type": "thinking_delta", "thinking": block["thinking"]}]
        else:
            start = {**block, "input": {}}
            deltas = [
                {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block["input"]),
                }
            ]
        events += [
            {"type": "content_block_start", "index": index, "content_block": start},
            *(
                {"type": "content_block_delta", "index": index, "delta": delta}
                for delta in deltas
            ),
            {"type": "content_block_stop", "index": index},
        ]
    tool = any(
        isinstance(block, dict) and block["type"] == "tool_use" for block in blocks
    )
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if tool else "end_turn"},
            "usage": {"output_tokens": 11},
        },
        {"type": "message_stop"},
    ]
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
    ).encode()


def _tool_use(id: str, name: str, **input: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ScriptedApi]:
    """The scripted API, with Claude Code pointed at it, a config dir, and a workspace under tmp_path."""
    api = ScriptedApi()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", api.url)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.chdir(workspace)
    yield api
    api.server.shutdown()


@pytest_asyncio.fixture
async def env(api: ScriptedApi) -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local(plugins=[AgentHarnessPlugin()])
    yield env
    await env.shutdown()


def _worker(
    env: WorkflowEnvironment, task_queue: str, connector: Any = None
) -> Worker:
    """A Worker for the test agent, with the Claude Agent SDK plugin and its ``connector``."""
    return Worker(
        env.client,
        task_queue=task_queue,
        workflows=[ClaudeSessionTestAgent],
        plugins=[ClaudeAgentSDKPlugin(connector=connector)],
    )


async def _start(env: WorkflowEnvironment, task_queue: str) -> WorkflowHandle[Any, Any]:
    return await env.client.start_workflow(
        ClaudeSessionTestAgent.run,
        AgentConfig(),
        id=task_queue,
        task_queue=task_queue,
    )


async def _replay(handle: WorkflowHandle[Any, Any]) -> None:
    """Replay the agent's history, which fails if it is not deterministic."""
    replayer = Replayer(
        workflows=[ClaudeSessionTestAgent],
        plugins=[AgentHarnessPlugin(), ClaudeAgentSDKPlugin()],
    )
    await replayer.replay_workflow(await handle.fetch_history())


@pytest_asyncio.fixture
async def agent(
    env: WorkflowEnvironment,
) -> AsyncIterator[tuple[Client, WorkflowHandle[Any, Any]]]:
    """A running test agent on its own Worker. Replays its history after."""
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue):
        handle = await _start(env, task_queue)
        yield env.client, handle
        await handle.terminate()
    await _replay(handle)


async def _send(handle: WorkflowHandle[Any, Any], text: str) -> AgentMessageReply:
    return await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": text}),
        result_type=AgentMessageReply,
    )


async def _turn(
    agent: tuple[Client, WorkflowHandle[Any, Any]],
    text: str,
    *,
    approve: bool = True,
    callback_result: str | None = None,
) -> list[AgentEvent]:
    """Send ``text`` and return its turn's events, deciding each approval it asks for with
    ``approve`` and answering each callback with ``callback_result``, as the client would."""
    reply = await _send(agent[1], text)
    return await _turn_events(
        agent, reply.turn_number, approve=approve, callback_result=callback_result
    )


async def _turn_events(
    agent: tuple[Client, WorkflowHandle[Any, Any]],
    turn_number: int,
    *,
    approve: bool = True,
    callback_result: str | None = None,
    unanswered: Container[str] = (),
    denied: Container[str] = (),
) -> list[AgentEvent]:
    """Turn ``turn_number``'s events, until it ends, deciding its approvals and callbacks as
    :func:`_turn` does, but leaving the ``unanswered`` tool ids' waiting and denying the
    ``denied`` ones."""
    client, handle = agent
    events: list[AgentEvent] = []
    # The turn's subagents, whose events carry their own turns.
    subagents: set[str] = set()
    async with asyncio.timeout(90):
        async for item in WorkflowStreamClient.create(client, handle.id).subscribe(
            topics=[TURN_EVENTS_TOPIC],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            event = item.data
            if event.agent_id not in subagents and event.turn_number != turn_number:
                continue
            events.append(event)
            if event.event.type == E.SUBAGENT_STARTED:
                subagents.add(event.event.subagent_id)
            if event.event.type == E.TURN_END and event.agent_id not in subagents:
                return events
            if getattr(event.event, "tool_id", None) in unanswered:
                continue
            if event.event.type == E.TOOL_APPROVAL_REQUESTED and any(
                # Not one decided before a Worker restart.
                a.tool_id == event.event.tool_id
                for a in (await AgentClient(client, handle.id).get_status()).pending_approvals
            ):
                await handle.execute_update(
                    TOOL_APPROVAL_UPDATE,
                    ToolApprovalDecision(
                        tool_id=event.event.tool_id,
                        approved=approve and event.event.tool_id not in denied,
                    ),
                    result_type=ToolApprovalResult,
                )
            elif event.event.type == E.CALLBACK_REQUESTED:
                await AgentClient(client, handle.id).provide_callback_result(
                    event.event.tool_id, result=callback_result
                )
    raise AssertionError("unreachable")


def _tool_events(events: list[AgentEvent], tool_id: str) -> list[AgentEventType]:
    return [
        e.event.type for e in events if getattr(e.event, "tool_id", None) == tool_id
    ]


def _of(events: list[AgentEvent], type: AgentEventType) -> list[Any]:
    return [e.event for e in events if e.event.type == type]


async def test_an_approved_call_runs_and_the_next_turn_resumes_the_session(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {
        "write it": _tool_use("toolu_1", "Bash", command="echo hi > notes.txt"),
        "tool_result:toolu_1": "Wrote it.",
        "read it": _tool_use("toolu_2", "Bash", command="cat notes.txt"),
        "tool_result:toolu_2": "Read it.",
    }

    first = await _turn(agent, "write it")

    assert _tool_events(first, "toolu_1") == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.TOOL_END,
    ]
    # Two model calls, the first ending at its tool request. Each counts its input, cache
    # included; the turn's output, which Claude Code reports only on its result, is the last's.
    calls = [
        (e.event.type, e.event.model_dump().get("usage"))
        for e in first
        if e.event.type
        in (E.MODEL_INTERACTION_STARTED, E.MODEL_INTERACTION_ENDED, E.TOOL_REQUESTED)
    ]
    usage = {"input_tokens": 15, "cached_tokens": 7}
    assert [
        (t, u and {k: v for k, v in u.items() if v is not None}) for t, u in calls
    ] == [
        (E.MODEL_INTERACTION_STARTED, None),
        (E.MODEL_INTERACTION_ENDED, usage),
        (E.TOOL_REQUESTED, None),
        (E.MODEL_INTERACTION_STARTED, None),
        (E.MODEL_INTERACTION_ENDED, {**usage, "output_tokens": 22}),
    ]
    assert Path("notes.txt").read_text() == "hi\n"

    second = await _turn(agent, "read it")

    [end] = _of(second, E.TOOL_END)
    assert (end.tool_id, end.tool_name, end.tool_output) == ("toolu_2", "Bash", "hi")
    # A new Claude Code resumed the session: it sent the first turn's prompt along with the second.
    request = next(r for r in api.requests if "read it" in _prompt_of(r))
    assert "write it" in json.dumps(request["messages"])


async def test_a_denied_call_never_runs_and_claude_gets_the_denial(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {
        "write it": _tool_use("toolu_1", "Bash", command="echo hi > notes.txt"),
        "tool_result:toolu_1": "It was denied.",
    }

    events = await _turn(agent, "write it", approve=False)

    # As in the other integrations, a denied call never starts, so it gets no tool_start or
    # tool_error.
    assert _tool_events(events, "toolu_1") == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
    ]
    assert not Path("notes.txt").exists()
    assert [d.text for d in _of(events, E.REPLY_DELTA)] == ["It ", "was ", "denied."]


async def test_the_same_call_twice_in_a_turn_is_decided_twice(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {
        "write it": _tool_use("toolu_1", "Bash", command="echo hi >> notes.txt"),
        "tool_result:toolu_1": _tool_use(
            "toolu_2", "Bash", command="echo hi >> notes.txt"
        ),
        "tool_result:toolu_2": "Wrote it twice.",
    }

    events = await _turn(agent, "write it")

    # The first call's approval doesn't decide the second.
    assert [e.tool_id for e in _of(events, E.TOOL_APPROVAL_REQUESTED)] == [
        "toolu_1",
        "toolu_2",
    ]
    assert Path("notes.txt").read_text() == "hi\nhi\n"


async def test_parallel_calls_end_before_the_next_model_call_starts(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {
        "run both": [
            _tool_use("toolu_1", "Bash", command="echo a"),
            _tool_use("toolu_2", "Bash", command="sleep 1; echo b"),
            _tool_use("toolu_3", "mcp__harness__shout", text="c"),
        ],
        "tool_result:": "Ran both.",
    }

    events = await _turn(agent, "run both")

    # Claude answers the calls' results together, once they all ended, as other SDKs do.
    types = [e.event.type for e in events]
    assert types.count(E.MODEL_INTERACTION_STARTED) == 2
    second = len(types) - 1 - types[::-1].index(E.MODEL_INTERACTION_STARTED)
    last_end = max(i for i, t in enumerate(types) if t in (E.TOOL_END, E.TOOL_ERROR))
    assert last_end < second


async def test_a_call_claude_code_runs_unasked_is_gated_too(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    # Claude Code reads a file in its working directory without asking; the harness still decides.
    Path("notes.txt").write_text("secret")
    api.answers = {
        "read it": _tool_use(
            "toolu_1", "Read", file_path=str(Path("notes.txt").resolve())
        ),
        "tool_result:toolu_1": "It was denied.",
    }

    events = await _turn(agent, "read it", approve=False)

    assert _tool_events(events, "toolu_1") == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
    ]
    request = next(r for r in api.requests if "tool_result:toolu_1" in _prompt_of(r))
    assert "secret" not in json.dumps(request["messages"])


async def test_claudes_thinking_and_text_stream_once(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {"think": [{"type": "thinking", "thinking": "Hmm."}, "All done."]}

    events = await _turn(agent, "think")

    # Each delta as Claude streams it, and not the whole blocks again after.
    assert [s.delta for s in _of(events, E.THOUGHT_SUMMARY)] == [{"content": "Hmm."}]
    assert [d.text for d in _of(events, E.REPLY_DELTA)] == ["All ", "done."]


async def test_a_subagents_calls_reach_the_turn_stream(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {
        "delegate it": _tool_use(
            "toolu_agent",
            "Agent",
            description="Write the notes",
            prompt="write the notes",
            subagent_type="writer",
        ),
        "write the notes": _tool_use(
            "toolu_bash", "Bash", command="echo hi > notes.txt"
        ),
        "tool_result:toolu_bash": "Wrote notes.txt.",
        "tool_result:toolu_agent": "It wrote notes.txt.",
    }

    events = await _turn(agent, "delegate it")

    # The Agent call is gated too.
    assert _tool_events(events, "toolu_agent") == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.TOOL_END,
    ]
    # The subagent runs inline: its events sit in a bracket, under its own agent_id.
    root = events[0].agent_id
    [started] = _of(events, E.SUBAGENT_STARTED)
    [sent] = _of(events, E.SUBAGENT_MESSAGE_SENT)
    [received] = _of(events, E.SUBAGENT_REPLY_RECEIVED)
    [stopped] = _of(events, E.SUBAGENT_STOPPED)
    assert (sent.inline, sent.workflow_id, sent.agent_key, sent.subagent_turn) == (
        True,
        "toolu_agent",
        "writer",
        1,
    )
    assert (received.inline, received.subagent_id, received.outcome) == (
        True,
        sent.subagent_id,
        "ok",
    )
    # It starts and stops as a child workflow does.
    for marker in (started, stopped):
        assert (marker.inline, marker.subagent_id, marker.workflow_id) == (
            True,
            sent.subagent_id,
            "toolu_agent",
        )
    assert re.fullmatch(rf"{root}-[0-9a-f]{{6}}", sent.subagent_id)
    types = [e.event.type for e in events]
    assert types.index(E.SUBAGENT_STARTED) + 1 == types.index(E.SUBAGENT_MESSAGE_SENT)
    assert types.index(E.SUBAGENT_REPLY_RECEIVED) + 1 == types.index(E.SUBAGENT_STOPPED)
    bracket = events[
        types.index(E.SUBAGENT_MESSAGE_SENT) + 1 : types.index(
            E.SUBAGENT_REPLY_RECEIVED
        )
    ]
    # The subagent's task, turn, model calls and report, as a child workflow's, and its gated
    # call, whose approval is the subagent's too, though this agent decides it.
    child = sent.subagent_id
    assert [(e.event.type, e.agent_id) for e in bracket] == [
        (E.MESSAGE_ACCEPTED, child),
        (E.TURN_STARTED, child),
        (E.MESSAGE_HANDLER_START, child),
        (E.MODEL_INTERACTION_STARTED, child),
        (E.MODEL_INTERACTION_ENDED, child),
        (E.TOOL_REQUESTED, child),
        (E.TOOL_APPROVAL_REQUESTED, child),
        (E.TOOL_APPROVAL_RESOLVED, child),
        (E.TOOL_START, child),
        (E.TOOL_END, child),
        (E.MODEL_INTERACTION_STARTED, child),
        (E.MODEL_INTERACTION_ENDED, child),
        (E.MESSAGE_HANDLER_END, child),
        (E.TURN_END, child),
    ]
    assert bracket[0].event.model_dump()["payload"] == {"prompt": "write the notes"}
    assert bracket[-2].event.model_dump()["output"] == {"text": "Wrote notes.txt."}
    # Its message is the Agent call, so its reply never pairs with this agent's message.
    assert {
        e.message_id
        for e in bracket
        if e.event.type not in (E.TURN_STARTED, E.TURN_END)
    } == {"toolu_agent"}
    # Its events, its approval's included, are its own first turn's, not this agent's turn's.
    assert {(e.turn_id, e.turn_number) for e in bracket} == {(f"{child}-turn-1", 1)}
    assert Path("notes.txt").read_text() == "hi\n"
    # The Agent call's own events stay with this agent, and its result follows the bracket.
    assert {
        e.agent_id for e in events if getattr(e.event, "tool_id", None) == "toolu_agent"
    } == {root}
    assert types.index(E.SUBAGENT_REPLY_RECEIVED) < max(
        i
        for i, e in enumerate(events)
        if getattr(e.event, "tool_id", None) == "toolu_agent"
    )
    # The subagent's own reply is the Agent tool's result, not part of the turn's reply.
    assert [
        e.event.text
        for e in events
        if e.event.type == E.REPLY_DELTA and e.agent_id == root
    ] == ["It ", "wrote ", "notes.txt."]


async def test_a_subagent_that_only_answers_is_a_subagent_too(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {
        "delegate it": _tool_use(
            "toolu_agent", "Agent", description="Say", prompt="say hi", subagent_type="writer"
        ),
        "say hi": "Hi from the writer.",
        "tool_result:toolu_agent": "It said hi.",
    }

    events = await _turn(agent, "delegate it")

    # Claude Code sends only the subagent's task: its reply is the Agent call's result.
    [sent] = _of(events, E.SUBAGENT_MESSAGE_SENT)
    [received] = _of(events, E.SUBAGENT_REPLY_RECEIVED)
    assert received.subagent_id == sent.subagent_id
    child = [e.event for e in events if e.agent_id == sent.subagent_id]
    assert [e.type for e in child] == [
        E.MESSAGE_ACCEPTED,
        E.TURN_STARTED,
        E.MESSAGE_HANDLER_START,
        E.MODEL_INTERACTION_STARTED,
        E.MODEL_INTERACTION_ENDED,
        E.MESSAGE_HANDLER_END,
        E.TURN_END,
    ]
    assert child[-2].output == {"text": "Hi from the writer."}


async def test_a_harness_tool_runs_through_the_harness(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {
        "shout it": _tool_use("toolu_1", "mcp__harness__shout", text="hi"),
        "tool_result:toolu_1": "Shouted.",
    }

    events = await _turn(agent, "shout it")

    # The harness tool's own events, once each: Claude Code's tool_use adds no second card.
    [requested] = _of(events, E.TOOL_REQUESTED)
    assert (requested.tool_name, requested.tool_input) == ("shout", {"text": "hi"})
    assert _tool_events(events, requested.tool_id) == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.TOOL_END,
    ]
    [end] = _of(events, E.TOOL_END)
    assert end.tool_output == "HI"
    request = next(r for r in api.requests if "tool_result:toolu_1" in _prompt_of(r))
    assert "HI" in json.dumps(request["messages"])


async def test_a_subagents_harness_tool_call_is_the_subagents(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {
        "delegate it": _tool_use(
            "toolu_agent",
            "Agent",
            description="Shout",
            prompt="shout hi",
            subagent_type="writer",
        ),
        "shout hi": _tool_use("toolu_shout", "mcp__harness__shout", text="hi"),
        "tool_result:toolu_shout": "Shouted.",
        "tool_result:toolu_agent": "It shouted.",
    }

    events = await _turn(agent, "delegate it")

    [sent] = _of(events, E.SUBAGENT_MESSAGE_SENT)
    [requested] = [r for r in _of(events, E.TOOL_REQUESTED) if r.tool_name == "shout"]
    shout = [e for e in events if getattr(e.event, "tool_id", None) == requested.tool_id]
    assert [e.event.type for e in shout] == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.TOOL_END,
    ]
    # All of it is the subagent's, its approval included.
    assert {(e.agent_id, e.message_id) for e in shout} == {
        (sent.subagent_id, "toolu_agent")
    }


async def test_a_callback_tool_is_answered_by_the_client(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {
        "ask them": _tool_use("toolu_1", "mcp__harness__ask_user", question="Name?"),
        "tool_result:toolu_1": "Hi, Ada.",
    }

    events = await _turn(agent, "ask them", callback_result="Ada")

    [requested] = _of(events, E.TOOL_REQUESTED)
    [callback] = _of(events, E.CALLBACK_REQUESTED)
    assert callback.tool_input == {"question": "Name?"}
    assert _tool_events(events, requested.tool_id) == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.CALLBACK_REQUESTED,
        E.CALLBACK_RESOLVED,
        E.TOOL_END,
    ]
    request = next(r for r in api.requests if "tool_result:toolu_1" in _prompt_of(r))
    assert "Ada" in json.dumps(request["messages"])


async def test_a_turn_finishes_after_a_worker_restart(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    asked = threading.Event()
    released = threading.Event()

    def held_reply(body: dict[str, Any]) -> str:
        # Hold the first request until the Worker is gone; answer the next Worker at once.
        if not asked.is_set():
            asked.set()
            released.wait()
        return "Hi there."

    api.answers = {"greet me": held_reply}
    task_queue = f"claude-session-{uuid.uuid4()}"
    try:
        async with _worker(env, task_queue):
            handle = await _start(env, task_queue)
            reply = await _send(handle, "greet me")
            assert await asyncio.to_thread(asked.wait, 60)
    finally:
        released.set()

    async with _worker(env, task_queue):
        events = await _turn_events((env.client, handle), reply.turn_number)
        await handle.terminate()

    [end] = _of(events, E.MESSAGE_HANDLER_END)
    assert end.output == {"text": "Hi there."}
    # The lost connection's model call ended too.
    assert len(_of(events, E.MODEL_INTERACTION_STARTED)) == len(
        _of(events, E.MODEL_INTERACTION_ENDED)
    )
    # The finished turn kept one copy of the prompt.
    finished = [r for r in api.requests if "greet me" in json.dumps(r["messages"])][-1]
    assert json.dumps(finished["messages"]).count("greet me") == 1
    await _replay(handle)


async def test_approvals_survive_a_worker_restart(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    asked = threading.Event()
    released = threading.Event()
    calls = iter(["toolu_1", "toolu_2"])

    def write_it(body: dict[str, Any]) -> dict[str, Any]:
        # Claude words the call's description differently when it repeats the call.
        tool_id = next(calls)
        return _tool_use(
            tool_id, "Bash", command="echo hi > notes.txt", description=f"Write ({tool_id})"
        )

    def held_result(body: dict[str, Any]) -> str:
        # Hold the first tool result until the Worker is gone; answer the next Worker at once.
        if not asked.is_set():
            asked.set()
            released.wait()
        return "Wrote it."

    api.answers = {"tool_result:": held_result, "write it": write_it}
    task_queue = f"claude-session-{uuid.uuid4()}"
    try:
        async with _worker(env, task_queue):
            handle = await _start(env, task_queue)
            reply = await _send(handle, "write it")
            # Approves the first call, and keeps reading the turn across the restart.
            turn = asyncio.create_task(
                _turn_events((env.client, handle), reply.turn_number)
            )
            assert await asyncio.to_thread(asked.wait, 60)
    finally:
        released.set()

    async with _worker(env, task_queue):
        events = await turn
        await handle.terminate()

    assert _tool_events(events, "toolu_1")[:2] == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
    ]
    # The second call ran on the first call's approval. If the first call's result didn't reach
    # the Worker before it went away, the second carries on as the first instead.
    assert [*_tool_events(events, "toolu_1")[2:], *_tool_events(events, "toolu_2")] in (
        [E.TOOL_APPROVAL_RESOLVED, E.TOOL_START, E.TOOL_END, E.TOOL_REQUESTED, E.TOOL_START, E.TOOL_END],
        [E.TOOL_APPROVAL_RESOLVED, E.TOOL_START, E.TOOL_END],
    )
    assert len(_of(events, E.TOOL_APPROVAL_REQUESTED)) == 1
    # The lost connection's model call ended too.
    assert len(_of(events, E.MODEL_INTERACTION_STARTED)) == len(
        _of(events, E.MODEL_INTERACTION_ENDED)
    )
    await _replay(handle)


@pytest.mark.parametrize(
    ("name", "input"),
    [("Bash", {"command": "echo hi"}), ("mcp__harness__shout", {"text": "hi"})],
)
async def test_an_approval_left_pending_by_a_worker_restart_is_answered_once(
    api: ScriptedApi, env: WorkflowEnvironment, name: str, input: dict[str, Any]
) -> None:
    calls = iter(["toolu_1", "toolu_2"])
    api.answers = {
        "tool_result:": "Done.",
        "act now": lambda body: _tool_use(next(calls), name, **input),
    }
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue):
        handle = await _start(env, task_queue)
        status = AgentClient(env.client, handle.id).get_status
        reply = await _send(handle, "act now")
        # The Worker goes away while the first call waits for its approval.
        async with asyncio.timeout(60):
            while not (pending := (await status()).pending_approvals):
                await asyncio.sleep(0.1)
    [first] = pending

    async with _worker(env, task_queue):
        # Approves the waiting approval, after the restart.
        events = await _turn_events((env.client, handle), reply.turn_number)
        left = (await status()).pending_approvals
        await handle.terminate()

    # One call and one approval: Claude Code's repeat carries on as the first call.
    assert _tool_events(events, first.tool_id) == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.TOOL_END,
    ]
    assert len(_of(events, E.TOOL_REQUESTED)) == 1
    assert not left
    await _replay(handle)


async def test_an_approval_claude_stops_asking_for_is_settled_when_the_turn_ends(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    commands = iter(["echo one", "echo two"])
    api.answers = {
        "tool_result:": "Done.",
        "act now": lambda body: _tool_use(
            f"toolu_{uuid.uuid4().hex}", "Bash", command=next(commands)
        ),
    }
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue):
        handle = await _start(env, task_queue)
        status = AgentClient(env.client, handle.id).get_status
        reply = await _send(handle, "act now")
        async with asyncio.timeout(60):
            while not (pending := (await status()).pending_approvals):
                await asyncio.sleep(0.1)
    [first] = pending

    async with _worker(env, task_queue):
        # After the restart Claude makes a different call instead.
        events = await _turn_events(
            (env.client, handle), reply.turn_number, unanswered={first.tool_id}
        )
        left = (await status()).pending_approvals
        await handle.terminate()

    # The first call's approval is denied when the turn ends, as a cancelled call's is in the
    # other integrations: it never started, so it gets no tool_start or tool_error.
    _assert_abandoned(events, first.tool_id)
    assert not left
    assert len(_of(events, E.TOOL_END)) == 1
    await _replay(handle)


async def test_a_call_approved_after_claude_stops_asking_for_it_ends_when_the_turn_ends(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    commands = iter(["echo one", "echo two"])
    api.answers = {
        "tool_result:": "Done.",
        "act now": lambda body: _tool_use(
            f"toolu_{uuid.uuid4().hex}", "Bash", command=next(commands)
        ),
    }
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue):
        handle = await _start(env, task_queue)
        status = AgentClient(env.client, handle.id).get_status
        reply = await _send(handle, "act now")
        async with asyncio.timeout(60):
            while not (pending := (await status()).pending_approvals):
                await asyncio.sleep(0.1)
    [first] = pending

    async def approve(tool_id: str) -> None:
        await handle.execute_update(
            TOOL_APPROVAL_UPDATE,
            ToolApprovalDecision(tool_id=tool_id, approved=True),
            result_type=ToolApprovalResult,
        )

    async with _worker(env, task_queue):
        # Claude makes a different call instead after the restart; the first call is approved
        # once the new connection asks for its own, so no hook is left to start the first.
        turn = asyncio.create_task(
            _turn_events((env.client, handle), reply.turn_number, unanswered={first.tool_id})
        )
        async with asyncio.timeout(60):
            async for item in WorkflowStreamClient.create(env.client, handle.id).subscribe(
                topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
            ):
                event = item.data.event
                if event.type == E.TOOL_APPROVAL_REQUESTED and event.tool_id != first.tool_id:
                    break
        await approve(first.tool_id)
        events = await turn
        await handle.terminate()

    # An approved call Claude Code never ran still ends, so its card doesn't stay open.
    assert _tool_events(events, first.tool_id) == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_ERROR,
    ]
    await _replay(handle)


async def test_a_callback_left_pending_by_a_worker_restart_is_answered_once(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    calls = iter(["toolu_1", "toolu_2"])
    api.answers = {
        "tool_result:": "Hi, Ada.",
        "ask them": lambda body: _tool_use(
            next(calls), "mcp__harness__ask_user", question="Name?"
        ),
    }
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue):
        handle = await _start(env, task_queue)
        status = AgentClient(env.client, handle.id).get_status
        reply = await _send(handle, "ask them")
        # The Worker goes away while the first call waits for the client's result.
        async with asyncio.timeout(60):
            while not (pending := (await status()).pending_callbacks):
                for approval in (await status()).pending_approvals:
                    await handle.execute_update(
                        TOOL_APPROVAL_UPDATE,
                        ToolApprovalDecision(tool_id=approval.tool_id, approved=True),
                        result_type=ToolApprovalResult,
                    )
                await asyncio.sleep(0.1)
    [first] = pending

    async with _worker(env, task_queue):
        # Answers the waiting callback, after the restart.
        events = await _turn_events(
            (env.client, handle), reply.turn_number, callback_result="Ada"
        )
        left = (await status()).pending_callbacks
        await handle.terminate()

    # One call, approved and answered once: Claude Code's repeat gets its result.
    assert _tool_events(events, first.tool_id) == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.CALLBACK_REQUESTED,
        E.CALLBACK_RESOLVED,
        E.TOOL_END,
    ]
    assert len(_of(events, E.TOOL_REQUESTED)) == 1
    assert not left
    await _replay(handle)


async def test_identical_calls_decided_differently_keep_their_decisions_across_a_restart(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    asked = threading.Event()
    released = threading.Event()
    calls = iter(["toolu_1", "toolu_2", "toolu_3", "toolu_4"])

    def write_it(body: dict[str, Any]) -> dict[str, Any]:
        return _tool_use(next(calls), "Bash", command="echo hi >> notes.txt")

    def held_result(body: dict[str, Any]) -> str:
        # Hold the second tool result until the Worker is gone.
        asked.set()
        released.wait()
        return "Done."

    api.answers = {
        "tool_result:toolu_2": held_result,
        "tool_result:toolu_4": "Done.",
        "tool_result:": write_it,
        "write it": write_it,
    }
    task_queue = f"claude-session-{uuid.uuid4()}"
    try:
        async with _worker(env, task_queue):
            handle = await _start(env, task_queue)
            reply = await _send(handle, "write it")
            # Approves the first call and denies the second, the same one.
            turn = asyncio.create_task(
                _turn_events((env.client, handle), reply.turn_number, denied={"toolu_2"})
            )
            assert await asyncio.to_thread(asked.wait, 60)
    finally:
        released.set()

    async with _worker(env, task_queue):
        events = await turn
        await handle.terminate()

    # The re-run's calls get the decisions in the order they were made.
    assert _tool_events(events, "toolu_3") == [E.TOOL_REQUESTED, E.TOOL_START, E.TOOL_END]
    # The denied call never starts or errors, and its repeat shows no card of its own, so no card
    # is left open.
    assert _tool_events(events, "toolu_2") == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
    ]
    assert not _tool_events(events, "toolu_4")
    assert len(_of(events, E.TOOL_APPROVAL_REQUESTED)) == 2
    await _replay(handle)


async def test_a_call_cut_off_by_a_worker_restart_ends_once(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    calls = iter(["toolu_1", "toolu_2"])

    def wait_for_go(body: dict[str, Any]) -> dict[str, Any]:
        command = "touch started; while [ ! -f go ]; do sleep 0.1; done"
        return _tool_use(next(calls), "Bash", command=command)

    api.answers = {"tool_result:": "Done.", "wait for it": wait_for_go}
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue):
        handle = await _start(env, task_queue)
        reply = await _send(handle, "wait for it")
        turn = asyncio.create_task(
            _turn_events((env.client, handle), reply.turn_number)
        )
        # The Worker goes away while the first call runs.
        async with asyncio.timeout(60):
            while not Path("started").exists():
                await asyncio.sleep(0.1)
    Path("go").touch()

    async with _worker(env, task_queue):
        events = await turn
        await handle.terminate()

    # Claude Code runs the command again, as the first call, which ends with the repeat's result.
    assert _tool_events(events, "toolu_1") == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.TOOL_END,
    ]
    assert not _tool_events(events, "toolu_2")
    await _replay(handle)


async def test_a_subagent_cut_off_by_a_worker_restart_carries_on_as_one_subagent(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    calls = iter(["toolu_1", "toolu_2", "toolu_3"])
    # Claude words the subagent's task differently when it repeats the call.
    agents = iter([("toolu_agent", "block here"), ("toolu_agent2", "please block here")])

    def wait_for_go(body: dict[str, Any]) -> dict[str, Any]:
        command = "touch started; while [ ! -f go ]; do sleep 0.1; done"
        return _tool_use(next(calls), "Bash", command=command)

    def delegate(body: dict[str, Any]) -> dict[str, Any]:
        tool_id, prompt = next(agents)
        return _tool_use(tool_id, "Agent", description="Wait", prompt=prompt, subagent_type="writer")

    api.answers = {
        "tool_result:toolu_agent": "Done.",
        "tool_result:": "Waited.",
        "wait for it": delegate,
        "block here": wait_for_go,
    }
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue):
        handle = await _start(env, task_queue)
        reply = await _send(handle, "wait for it")
        turn = asyncio.create_task(
            _turn_events((env.client, handle), reply.turn_number)
        )
        # The Worker goes away while the subagent's call runs.
        async with asyncio.timeout(60):
            while not Path("started").exists():
                await asyncio.sleep(0.1)
    Path("go").touch()

    async with _worker(env, task_queue):
        events = await turn
        await handle.terminate()

    # Claude Code's repeat carries on as the first Agent call and the first subagent, whose
    # repeated call carries on as its first call.
    [started] = _of(events, E.SUBAGENT_STARTED)
    child = started.subagent_id
    markers = [
        (e.event.type, e.event.workflow_id, getattr(e.event, "outcome", None))
        for e in events
        if getattr(e.event, "subagent_id", None) == child
    ]
    assert markers == [
        (E.SUBAGENT_STARTED, "toolu_agent", None),
        (E.SUBAGENT_MESSAGE_SENT, "toolu_agent", None),
        (E.SUBAGENT_REPLY_RECEIVED, "toolu_agent", "ok"),
        (E.SUBAGENT_STOPPED, "toolu_agent", None),
    ]
    for tool_id in ("toolu_agent", "toolu_1"):
        assert _tool_events(events, tool_id) == [
            E.TOOL_REQUESTED,
            E.TOOL_APPROVAL_REQUESTED,
            E.TOOL_APPROVAL_RESOLVED,
            E.TOOL_START,
            E.TOOL_END,
        ]
    assert len(_of(events, E.TOOL_REQUESTED)) == 2
    # One child turn, which the dead run's model call doesn't leave open.
    types = [e.event.type for e in events if e.agent_id == child]
    assert types[:3] == [E.MESSAGE_ACCEPTED, E.TURN_STARTED, E.MESSAGE_HANDLER_START]
    assert types[-2:] == [E.MESSAGE_HANDLER_END, E.TURN_END]
    assert types.count(E.TURN_STARTED) == 1
    assert types.count(E.MODEL_INTERACTION_STARTED) == types.count(E.MODEL_INTERACTION_ENDED)
    assert {e.message_id for e in events if e.agent_id == child} - {None} == {"toolu_agent"}
    await _replay(handle)


async def test_a_subagents_cut_off_call_it_doesnt_repeat_ends_with_the_subagent(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    agents = iter(["toolu_agent", "toolu_agent2"])
    commands = iter(["echo one", "echo two"])
    api.answers = {
        "tool_result:toolu_agent": "Done.",
        "tool_result:": "Ran it.",
        "run it": lambda body: _tool_use(
            next(agents), "Agent", description="Run", prompt="run a command", subagent_type="writer"
        ),
        "run a command": lambda body: _tool_use(
            f"toolu_{uuid.uuid4().hex}", "Bash", command=next(commands)
        ),
    }
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue):
        handle = await _start(env, task_queue)
        status = AgentClient(env.client, handle.id).get_status
        reply = await _send(handle, "run it")
        # The Worker goes away while the subagent's call waits for its approval.
        async with asyncio.timeout(60):
            while not (
                pending := [a for a in (await status()).pending_approvals if a.tool_name == "Bash"]
            ):
                for approval in (await status()).pending_approvals:
                    if approval.tool_name == "Agent":
                        await handle.execute_update(
                            TOOL_APPROVAL_UPDATE,
                            ToolApprovalDecision(tool_id=approval.tool_id, approved=True),
                            result_type=ToolApprovalResult,
                        )
                await asyncio.sleep(0.1)
    [first] = pending

    async with _worker(env, task_queue):
        # After the restart the subagent makes a different call instead.
        events = await _turn_events(
            (env.client, handle), reply.turn_number, unanswered={first.tool_id}
        )
        left = (await status()).pending_approvals
        await handle.terminate()

    # The first call's approval is denied before the subagent ends, and the call never started.
    _assert_abandoned(events, first.tool_id)
    assert not left
    [started] = _of(events, E.SUBAGENT_STARTED)
    types = [
        (e.event.type, getattr(e.event, "tool_id", None))
        for e in events
        if e.agent_id == started.subagent_id
    ]
    assert types.index((E.TOOL_APPROVAL_RESOLVED, first.tool_id)) < types.index(
        (E.TURN_END, None)
    )
    await _replay(handle)


def _assert_abandoned(events: list[AgentEvent], tool_id: str) -> None:
    """``tool_id``'s approval was abandoned, as a cancelled pending approval is in the other
    integrations: denied, with no tool_start or tool_error."""
    assert _tool_events(events, tool_id) == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
    ]
    [resolved] = [
        e for e in _of(events, E.TOOL_APPROVAL_RESOLVED) if e.tool_id == tool_id
    ]
    assert (resolved.approved, resolved.reason) == (False, "approval unresolved")


async def test_a_denial_forced_on_a_dead_subagents_call_is_not_reused_by_another_call(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    agents = iter(["toolu_agent", "toolu_agent2"])
    commands = iter(["echo one", "echo two"])
    api.answers = {
        "tool_result:toolu_main": "Done.",
        # Once the subagent ends, this agent makes the call the subagent abandoned.
        "tool_result:toolu_agent": _tool_use("toolu_main", "Bash", command="echo one"),
        "tool_result:": "Ran it.",
        "run it": lambda body: _tool_use(
            next(agents), "Agent", description="Run", prompt="run a command", subagent_type="writer"
        ),
        "run a command": lambda body: _tool_use(
            f"toolu_{uuid.uuid4().hex}", "Bash", command=next(commands)
        ),
    }
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue):
        handle = await _start(env, task_queue)
        status = AgentClient(env.client, handle.id).get_status
        reply = await _send(handle, "run it")
        # The Worker goes away while the subagent's call waits for its approval.
        async with asyncio.timeout(60):
            while not (
                pending := [a for a in (await status()).pending_approvals if a.tool_name == "Bash"]
            ):
                for approval in (await status()).pending_approvals:
                    if approval.tool_name == "Agent":
                        await handle.execute_update(
                            TOOL_APPROVAL_UPDATE,
                            ToolApprovalDecision(tool_id=approval.tool_id, approved=True),
                            result_type=ToolApprovalResult,
                        )
                await asyncio.sleep(0.1)
    [first] = pending

    async with _worker(env, task_queue):
        events = await _turn_events(
            (env.client, handle), reply.turn_number, unanswered={first.tool_id}
        )
        await handle.terminate()

    # This agent's identical call is asked about and runs, instead of getting the denial the
    # subagent's end forced on the abandoned call.
    assert _tool_events(events, "toolu_main") == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.TOOL_END,
    ]
    await _replay(handle)


@pytest.mark.parametrize(
    ("name", "input"),
    [("Bash", {"command": "echo hi"}), ("mcp__harness__shout", {"text": "hi"})],
)
async def test_a_finished_call_is_not_reused_by_another_agents_identical_call(
    api: ScriptedApi, env: WorkflowEnvironment, name: str, input: dict[str, Any]
) -> None:
    asked = threading.Event()
    released = threading.Event()
    # This agent makes the call; after the restart it delegates instead, and its subagent makes it.
    first = iter(
        [
            _tool_use("toolu_1", name, **input),
            _tool_use(
                "toolu_agent", "Agent", description="Run", prompt="run it too", subagent_type="writer"
            ),
        ]
    )

    def held_result(body: dict[str, Any]) -> str:
        # Hold the first call's result until the Worker is gone.
        asked.set()
        released.wait()
        return "Done."

    api.answers = {
        "tool_result:toolu_1": held_result,
        "tool_result:": "Done.",
        "run it too": _tool_use("toolu_2", name, **input),
        "run it": lambda body: next(first),
    }
    task_queue = f"claude-session-{uuid.uuid4()}"
    try:
        async with _worker(env, task_queue):
            handle = await _start(env, task_queue)
            reply = await _send(handle, "run it")
            turn = asyncio.create_task(
                _turn_events((env.client, handle), reply.turn_number)
            )
            assert await asyncio.to_thread(asked.wait, 60)
    finally:
        released.set()

    async with _worker(env, task_queue):
        events = await turn
        await handle.terminate()

    # The subagent's call is its own: asked about and run, not given this agent's result.
    [started] = _of(events, E.SUBAGENT_STARTED)
    [requested] = [
        e.event
        for e in events
        if e.event.type == E.TOOL_REQUESTED and e.agent_id == started.subagent_id
    ]
    assert _tool_events(events, requested.tool_id) == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.TOOL_END,
    ]
    await _replay(handle)


async def test_a_subagents_cut_off_harness_call_it_doesnt_repeat_ends_with_the_subagent(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    agents = iter(["toolu_agent", "toolu_agent2"])
    calls = iter(
        [
            _tool_use("toolu_shout", "mcp__harness__shout", text="hi"),
            _tool_use("toolu_bash", "Bash", command="echo two"),
        ]
    )
    api.answers = {
        "tool_result:toolu_agent": "Done.",
        "tool_result:": "Ran it.",
        "run it": lambda body: _tool_use(
            next(agents), "Agent", description="Run", prompt="run a command", subagent_type="writer"
        ),
        "run a command": lambda body: next(calls),
    }
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue):
        handle = await _start(env, task_queue)
        status = AgentClient(env.client, handle.id).get_status
        reply = await _send(handle, "run it")
        # The Worker goes away while the subagent's harness call waits for its approval.
        async with asyncio.timeout(60):
            while not (
                pending := [a for a in (await status()).pending_approvals if a.tool_name == "shout"]
            ):
                for approval in (await status()).pending_approvals:
                    if approval.tool_name == "Agent":
                        await handle.execute_update(
                            TOOL_APPROVAL_UPDATE,
                            ToolApprovalDecision(tool_id=approval.tool_id, approved=True),
                            result_type=ToolApprovalResult,
                        )
                await asyncio.sleep(0.1)
    [first] = pending

    async with _worker(env, task_queue):
        # After the restart the subagent makes a different call instead.
        events = await _turn_events(
            (env.client, handle), reply.turn_number, unanswered={first.tool_id}
        )
        left = (await status()).pending_approvals
        await handle.terminate()

    # The harness call's approval is denied before the subagent ends, and the call never started.
    _assert_abandoned(events, first.tool_id)
    assert not left
    [started] = _of(events, E.SUBAGENT_STARTED)
    types = [
        (e.event.type, getattr(e.event, "tool_id", None))
        for e in events
        if e.agent_id == started.subagent_id
    ]
    assert types.index((E.TOOL_APPROVAL_RESOLVED, first.tool_id)) < types.index(
        (E.TURN_END, None)
    )
    await _replay(handle)


async def test_the_transport_gets_the_sessions_activity_options_and_stream(
    api: ScriptedApi, agent: tuple[Client, WorkflowHandle[Any, Any]]
) -> None:
    api.answers = {"hi": "Hello there."}

    await _turn(agent, "hi")

    client, handle = agent
    [connect] = [
        e.activity_task_scheduled_event_attributes
        for e in (await handle.fetch_history()).events
        if e.HasField("activity_task_scheduled_event_attributes")
        and e.activity_task_scheduled_event_attributes.activity_type.name
        == "claude-agent-sdk-connect"
    ]
    assert connect.start_to_close_timeout.ToTimedelta() == CONNECT_TIMEOUT
    # Claude Code's stream events reach the session's topic, the reply's text among them.
    text = ""
    async with asyncio.timeout(30):
        async for item in WorkflowStreamClient.create(client, handle.id).subscribe(
            topics=[CLAUDE_STREAM_TOPIC],
            from_offset=0,
            result_type=dict,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            delta = item.data["event"].get("delta", {})
            text += delta.get("text", "")
            if item.data["event"]["type"] == "message_stop":
                break
    assert text == "Hello there."


class _RecordingConnector:
    """Starts Claude Code in a subprocess, as the default connector does, and records each start."""

    def __init__(self) -> None:
        self.requests: list[ConnectActivityInput] = []

    async def connect(self, request: ConnectActivityInput) -> ClaudeCodeProcess:
        self.requests.append(request)
        return await SubprocessConnector().connect(request)


async def test_a_custom_connector_starts_claude_code(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    api.answers = {"hi": "Hello there."}
    connector = _RecordingConnector()
    task_queue = f"claude-session-{uuid.uuid4()}"

    async with _worker(env, task_queue, connector):
        handle = await _start(env, task_queue)
        events = await _turn((env.client, handle), "hi")
        await handle.terminate()

    [end] = _of(events, E.MESSAGE_HANDLER_END)
    assert end.output == {"text": "Hello there."}
    assert len(connector.requests) == 1
    await _replay(handle)


class _KeepingConnector:
    """Starts Claude Code as the default connector does, and keeps each process."""

    def __init__(self) -> None:
        self.processes: list[Any] = []

    async def connect(self, request: ConnectActivityInput) -> ClaudeCodeProcess:
        process = await SubprocessConnector().connect(request)
        self.processes.append(process)
        return process


async def test_a_turn_carries_on_after_claude_code_dies(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    calls = iter(["toolu_1", "toolu_2"])
    api.answers = {
        "tool_result:": "Done.",
        "act now": lambda body: _tool_use(next(calls), "Bash", command="echo hi"),
    }
    connector = _KeepingConnector()
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue, connector):
        handle = await _start(env, task_queue)
        status = AgentClient(env.client, handle.id).get_status
        reply = await _send(handle, "act now")
        async with asyncio.timeout(60):
            while not (await status()).pending_approvals:
                await asyncio.sleep(0.1)
        # Claude Code dies while the call waits for its approval; the Worker stays.
        connector.processes[0]._process.kill()
        events = await _turn_events((env.client, handle), reply.turn_number)
        await handle.terminate()

    [end] = _of(events, E.MESSAGE_HANDLER_END)
    assert end.output == {"text": "Done."}
    assert len(connector.processes) == 2
    # One call and one approval: Claude Code's repeat carries on as the first call.
    assert _tool_events(events, "toolu_1") == [
        E.TOOL_REQUESTED,
        E.TOOL_APPROVAL_REQUESTED,
        E.TOOL_APPROVAL_RESOLVED,
        E.TOOL_START,
        E.TOOL_END,
    ]
    assert len(_of(events, E.TOOL_REQUESTED)) == 1
    await _replay(handle)


class _FailingConnector:
    """Fails to start Claude Code ``failures`` times, then starts it as the default does."""

    def __init__(self, failures: int) -> None:
        self.failures = failures

    async def connect(self, request: ConnectActivityInput) -> ClaudeCodeProcess:
        if self.failures:
            self.failures -= 1
            raise ProcessError("Claude Code failed to start", exit_code=1)
        return await SubprocessConnector().connect(request)


async def test_claude_code_failing_to_start_is_retried_with_a_backoff(
    api: ScriptedApi, env: WorkflowEnvironment
) -> None:
    api.answers = {"hi": "Hello there."}
    task_queue = f"claude-session-{uuid.uuid4()}"
    async with _worker(env, task_queue, _FailingConnector(2)):
        handle = await _start(env, task_queue)
        events = await _turn((env.client, handle), "hi")
        await handle.terminate()

    [end] = _of(events, E.MESSAGE_HANDLER_END)
    assert end.output == {"text": "Hello there."}
    history = (await handle.fetch_history()).events
    connects = [
        e
        for e in history
        if e.activity_task_scheduled_event_attributes.activity_type.name
        == "claude-agent-sdk-connect"
    ]
    # The first retry is at once, the next after a wait.
    timers = [
        e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
        for e in history
        if e.HasField("timer_started_event_attributes")
    ]
    assert len(connects) == 3
    assert timedelta(seconds=2) in timers
    await _replay(handle)



class _FakeRunner:
    """Records the events a session publishes, outside a workflow."""

    def __init__(self) -> None:
        self.current_stream_context = TurnStreamContext(
            turn_id="turn-1", turn_number=1, agent_id="root"
        )
        self.events: list[Any] = []
        self._inline_turns: dict[str, tuple[str, int]] = {}

    def _pub(self, turn_id: str, turn_number: int, event: Any, **scope: Any) -> None:
        self.events.append(event)

    def _fresh_subagent_handle(self) -> str:
        return "root-sub"


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stands in for ClaudeSDKClient: each connection answers from the next script in
    ``connections[i]["script"]`` (a message, or an exception to raise), and records its options
    and the text sent."""
    scripts: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, options: ClaudeAgentOptions, transport: Any) -> None:
            self.connection = next(c for c in scripts if "options" not in c)
            self.connection.update(options=options, sent=[])

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def query(self, text: str) -> None:
            self.connection["sent"].append(text)

        async def receive_response(self) -> AsyncIterator[Any]:
            for item in self.connection["script"]:
                if isinstance(item, BaseException):
                    raise item
                yield item

    monkeypatch.setattr(claude_agent_sdk_harness, "ClaudeSDKClient", FakeClient)
    return scripts


def _session(**options: Any) -> tuple[_FakeRunner, HarnessClaudeSession]:
    runner = _FakeRunner()
    return runner, HarnessClaudeSession(runner, ClaudeAgentOptions(**options))  # type: ignore[arg-type]


def _result(**fields: Any) -> ResultMessage:
    return ResultMessage(
        **{
            "subtype": "success",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "is_error": False,
            "num_turns": 1,
            "session_id": "s1",
            **fields,
        }
    )


_INIT = SystemMessage(subtype="init", data={"session_id": "s1"})


def _reply(uuid: str, *content: Any) -> AssistantMessage:
    return AssistantMessage(content=list(content), model="claude", uuid=uuid)


async def test_a_lost_connection_resumes_at_the_reply_before_the_turn_and_resends_the_prompt(
    connections: list[dict[str, Any]],
) -> None:
    connections += [
        {"script": [_INIT, _reply("r1", TextBlock("Hi.")), _result()]},
        # The prompt was saved, then the connection was lost.
        {"script": [_INIT, UserMessage(content="next", uuid="p2"), CLIConnectionError("lost")]},
        {"script": [_INIT, _reply("r2", TextBlock("Done.")), _result()]},
    ]
    _, session = _session()

    await session.run("first")
    await session.run("next")

    # No "continue" message for the model to misread: it sees the prompt once, as sent.
    retry = connections[2]
    assert (retry["options"].resume, retry["options"].resume_session_at) == ("s1", "r1")
    assert retry["sent"] == ["next"]


@pytest.mark.parametrize(
    "ending",
    [[ProcessError("Command failed with exit code -9", exit_code=-9)], []],
    ids=["killed", "exited"],
)
async def test_claude_code_ending_mid_turn_resumes_as_a_lost_connection_does(
    connections: list[dict[str, Any]], ending: list[Any]
) -> None:
    connections += [
        {"script": [_INIT, _reply("r1", TextBlock("Hi.")), _result()]},
        {"script": [_INIT, UserMessage(content="next", uuid="p2"), *ending]},
        {"script": [_INIT, _reply("r2", TextBlock("Done.")), _result()]},
    ]
    _, session = _session()

    await session.run("first")
    result = await session.run("next")

    retry = connections[2]
    assert (retry["options"].resume, retry["options"].resume_session_at) == ("s1", "r1")
    assert retry["sent"] == ["next"]
    assert result.session_id == "s1"


async def test_options_resume_seeds_the_session(
    connections: list[dict[str, Any]],
) -> None:
    connections += [
        {"script": [_INIT, UserMessage(content="hi", uuid="p1"), CLIConnectionError("lost")]},
        {"script": [_INIT, _reply("r1", TextBlock("Hi.")), _result()]},
    ]
    _, session = _session(resume="seeded")

    await session.run("hi")

    assert connections[0]["options"].resume == "seeded"
    # Its last reply is unknown, so the turn continues from the saved prompt.
    retry = connections[1]
    assert (retry["options"].resume, retry["options"].resume_session_at) == ("seeded", "p1")
    assert retry["sent"] == [claude_agent_sdk_harness._CONTINUE]


_DELEGATE = [
    _INIT,
    _reply(
        "r1",
        ToolUseBlock(
            "toolu_agent", "Agent", {"prompt": "write it", "subagent_type": "writer"}
        ),
    ),
    # The subagent's task opens its bracket.
    UserMessage(content=[TextBlock("write it")], parent_tool_use_id="toolu_agent"),
]


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"is_error": True, "result": "Overloaded"}, "Overloaded"),
        ({"subtype": "error_max_turns", "errors": ["Too many turns"]}, "Too many turns"),
    ],
    ids=["is_error", "error_subtype"],
)
async def test_an_error_result_fails_the_turn_and_ends_the_open_calls_and_subagents(
    connections: list[dict[str, Any]], fields: dict[str, Any], error: str
) -> None:
    connections += [{"script": [*_DELEGATE, _result(**fields)]}]
    runner, session = _session()

    # As a failed model call does in the other integrations: the handler fails.
    with pytest.raises(ApplicationError, match=error):
        await session.run("delegate it")

    _assert_ended(runner.events, error)


async def test_the_restart_backoff_resets_once_a_connection_makes_progress(
    connections: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def sleep(seconds: float, **_: Any) -> None:
        waits.append(seconds)

    monkeypatch.setattr(claude_agent_sdk_harness.workflow, "sleep", sleep)
    lost = CLIConnectionError("lost")
    connections += [
        {"script": [_INIT, lost]},
        {"script": [_INIT, lost]},
        # Claude replies, so this connection made progress.
        {"script": [_INIT, _reply("r1", TextBlock("Working.")), lost]},
        {"script": [_INIT, lost]},
        {"script": [_INIT, _reply("r2", TextBlock("Done.")), _result()]},
    ]
    _, session = _session()

    await session.run("hi")

    assert waits == [2, 2]


async def test_parallel_subagents_of_one_type_keep_their_calls_across_a_restart() -> None:
    calls = claude_agent_sdk_harness._TurnCalls()
    loop = asyncio.get_running_loop()
    inputs = [
        {"subagent_type": "writer", "description": "Write", "prompt": prompt}
        for prompt in ("write a", "write b")
    ]
    for tool_id, input in zip(["toolu_a", "toolu_b"], inputs):
        gate = calls.add("Agent", input, None, loop.create_future())  # type: ignore[arg-type]
        calls.cut_off[gate] = claude_agent_sdk_harness._CutOff(
            tool_id, None, f"sub-{tool_id}", lambda: None
        )
    observer = claude_agent_sdk_harness._ClaudeStreamObserver(
        _FakeRunner(),  # type: ignore[arg-type]
        calls,
    )

    # Claude repeats both calls, in the other order.
    observer.observe(
        _reply(
            "r2",
            ToolUseBlock("toolu_b2", "Agent", inputs[1]),
            ToolUseBlock("toolu_a2", "Agent", inputs[0]),
        )
    )

    assert observer._took_over == {"toolu_b2": "toolu_b", "toolu_a2": "toolu_a"}


async def test_a_failed_turn_ends_the_open_calls_and_subagents(
    connections: list[dict[str, Any]],
) -> None:
    connections += [{"script": [*_DELEGATE, ValueError("bad message")]}]
    runner, session = _session()

    with pytest.raises(ValueError):
        await session.run("delegate it")

    _assert_ended(runner.events, "bad message")


def _assert_ended(events: list[Any], error: str) -> None:
    [received] = [e for e in events if e.type == E.SUBAGENT_REPLY_RECEIVED]
    assert received.outcome == "error"
    assert [(e.tool_id, e.message) for e in events if e.type == E.TOOL_ERROR] == [
        ("toolu_agent", error)
    ]
    started = [e for e in events if e.type == E.MODEL_INTERACTION_STARTED]
    assert len(started) == len([e for e in events if e.type == E.MODEL_INTERACTION_ENDED])


async def test_a_subagent_without_a_structured_result_ends_with_its_results_text(
    connections: list[dict[str, Any]],
) -> None:
    connections += [
        {
            "script": [
                *_DELEGATE,
                # Claude Code sent no structured result for the subagent.
                UserMessage(
                    content=[ToolResultBlock("toolu_agent", [{"type": "text", "text": "Written."}])]
                ),
                _reply("r2", TextBlock("Done.")),
                _result(),
            ]
        }
    ]
    runner, session = _session()

    await session.run("delegate it")

    [end] = [e for e in runner.events if e.type == E.MESSAGE_HANDLER_END]
    assert end.output == {"text": "Written."}


async def test_a_harness_call_claude_code_answers_itself_is_not_left_for_a_handler(
    connections: list[dict[str, Any]],
) -> None:
    shout = ToolUseBlock("toolu_1", "mcp__harness__shout", {"text": "hi"})
    connections += [
        {
            "script": [
                _INIT,
                _reply("r1", shout),
                # Claude Code refused the call, so its handler never ran.
                UserMessage(content=[ToolResultBlock("toolu_1", "No.", is_error=True)]),
                _reply("r2", TextBlock("Okay.")),
                _result(),
            ]
        }
    ]
    runner, session = _session()

    await session.run("shout it")

    # A later identical call isn't matched to it, and Claude's answer is a model call.
    assert not session._observer._harness_calls
    started = [e for e in runner.events if e.type == E.MODEL_INTERACTION_STARTED]
    assert len(started) == 2
