# ABOUTME: Every shipped example carries its own conversation across a continue-as-new — and the
# one that deliberately does not is named here as a decision rather than an omission.
#
# tests/harness/test_continue_as_new.py covers the harness's half on a real server: a session
# rolls over and the agent still knows what was said. What it cannot cover is the half each
# example author writes, because @agent.snapshot / @agent.restore are per-SDK and the harness
# never looks inside the blob. These tests run the hooks the way the runner runs them — unbound,
# against the agent instance, with the blob carried through the same AgentConfig and the same
# data converter every entry point in this repo connects with — and then ask the restored agent
# to do something that only works if the conversation genuinely survived.
#
# The models are fakes, but they are real SDK models: the conversations here are produced by the
# SDK's own machinery and consumed by it again after the round trip, so what is under test is the
# actual serialization each example performs and not a hand-written approximation of it.
#
# Run with: uv run pytest tests/examples/test_rollover_hooks.py -v

from __future__ import annotations

import importlib
import os
import pkgutil
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

# examples.pydantic_ai_hello builds its TemporalAgent at module scope and Pydantic AI resolves
# the OpenAI provider eagerly, so importing it at all needs *a* key. Nothing here reaches the
# network: every model in this file is a local fake.
os.environ.setdefault("OPENAI_API_KEY", "not-a-real-key")

import agents as openai_agents
from agents import Agent as OpenAIAgent
from agents import Runner
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelResponse as PydanticModelResponse
from pydantic_ai.messages import TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio.contrib.pydantic import pydantic_data_converter

import examples
from temporal_agent_harness.harness.agent_protocol import AgentConfig, AgentResumeState
from temporal_agent_harness.harness.agent_workflow import (
    ResumptionHooks,
    _assert_json_native_snapshot,
    agent_resumption_hooks,
)

# Uploading traces would be the one thing in this file that touches the network.
openai_agents.set_tracing_disabled(True)


# ---------------------------------------------------------------------------
# Carrying a snapshot the way a rollover carries it
# ---------------------------------------------------------------------------


@pytest.fixture
def build(monkeypatch):
    """Construct an example agent outside a workflow, running its real ``@workflow.init``.

    The constructor is the point: ``@agent.restore`` runs after it, so what it must overwrite is
    the empty conversation the author's constructor sets, not a blank object. Getting there
    offline means stubbing the workflow APIs ``AgentWorkflowRunner.__init__`` touches — the same
    handful the offline fixtures in tests/harness/test_agent_workflow_runner.py stub.
    """
    import temporal_agent_harness.harness.agent_workflow as aw

    for handler in ("set_update_handler", "set_query_handler", "set_signal_handler"):
        monkeypatch.setattr(aw.workflow, handler, lambda *a, **k: None)
    monkeypatch.setattr(aw.workflow, "time", lambda: 0.0)
    monkeypatch.setattr(aw.workflow, "uuid4", lambda: uuid.uuid4())
    # Every example lets the runner build the stream — which it must, or the runner refuses to
    # roll the session over at all. Offline there is no workflow to build one in.
    monkeypatch.setattr(aw, "WorkflowStream", MagicMock())

    def construct(cls: type) -> Any:
        return cls(AgentConfig())

    return construct


async def _roll_over(cls: type, source: Any, target: Any) -> None:
    """Snapshot ``source``, carry the blob as a rollover carries it, and restore into ``target``.

    The carry is not a formality. The blob rides on ``AgentConfig.resume`` through Temporal's
    data converter, which is what turns "JSON-native" from a documented request into a real
    constraint: anything the converter cannot rebuild as plain data arrives at ``@agent.restore``
    as something other than what ``@agent.snapshot`` returned.

    Which is why the harness's own check runs here too, on the way past: it is what the rollover
    applies to the blob at ``_continue_as_new``, so applying it here means every example's hooks
    answer for it in a unit test rather than at somebody's hundredth turn.
    """
    hooks: ResumptionHooks | None = agent_resumption_hooks(cls)
    assert hooks is not None, f"{cls.__name__} declares no @agent.snapshot / @agent.restore pair"
    snapshot = _assert_json_native_snapshot(cls.__name__, hooks.snapshot(source))
    config = AgentConfig(resume=AgentResumeState(conversation=snapshot))
    payloads = await pydantic_data_converter.encode([config])
    carried = (await pydantic_data_converter.decode(payloads, [AgentConfig]))[0]
    assert carried.resume is not None and carried.resume.conversation is not None
    hooks.restore(target, carried.resume.conversation)


# ---------------------------------------------------------------------------
# The inventory: no example loses the feature by accident
# ---------------------------------------------------------------------------
#
# Rollover degrades silently — an agent with no hooks works exactly as it always did and simply
# never continues as new — so an example that forgets the pair is indistinguishable from one that
# chose to. This is the difference, written down.

_DELIBERATELY_HOOKLESS = {
    # A script runner with nothing to remember: each script is evaluated against the tools and
    # nothing about it is carried between turns. The pair would hand over an empty dict. It is
    # also always somebody's subagent — the parent it belongs to is where a long session
    # accumulates, and that parent does roll over.
    "MontyDynamicAgentWorkflow",
}


def _example_agent_classes() -> list[type]:
    """Every agent class in ``examples/``, found the way the runner finds one: by the handlers
    ``@agent.defn`` stamps on it at import."""
    found: dict[str, type] = {}
    for module_info in pkgutil.walk_packages(examples.__path__, "examples."):
        # Agent classes live in workflow modules; importing the workers and clients as well
        # would drag in their entry-point machinery for nothing.
        if not module_info.name.rsplit(".", 1)[-1].endswith("workflow"):
            continue
        module = importlib.import_module(module_info.name)
        for name, value in vars(module).items():
            if isinstance(value, type) and "__agent_handlers__" in vars(value):
                found[f"{value.__module__}.{name}"] = value
    return [found[key] for key in sorted(found)]


def test_every_example_agent_either_rolls_over_or_says_why():
    """The guard on the silent case. A new example that omits the pair fails here, which is the
    only place the omission is visible — the runtime is perfectly happy with it, right up until
    somebody's conversation gets long."""
    classes = _example_agent_classes()
    # The count is here so the walk cannot pass by finding nothing. Raise it when an example is
    # added; the assertion below is the one that matters.
    assert len(classes) >= 9, f"only found {len(classes)} example agents; the walk missed some"

    hookless = {cls.__name__ for cls in classes if agent_resumption_hooks(cls) is None}
    assert hookless == _DELIBERATELY_HOOKLESS


# ---------------------------------------------------------------------------
# OpenAI Agents SDK examples — the conversation is the SDK's input-item list
# ---------------------------------------------------------------------------


class _EchoModel(Model):
    """A model that answers with every user message it can see, in order.

    Which makes it a conversation detector: asked "bananas" with the earlier turn restored it
    says "apples bananas", and with the restore broken it says "bananas" — the same signal the
    end-to-end rollover test reads off a real reply.
    """

    async def get_response(self, system_instructions, input, *args, **kwargs) -> ModelResponse:
        said = " ".join(_user_texts(input))
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="msg-echo",
                    role="assistant",
                    status="completed",
                    type="message",
                    content=[ResponseOutputText(type="output_text", text=said, annotations=[])],
                )
            ],
            usage=Usage(),
            response_id=None,
        )

    def stream_response(self, *args, **kwargs):
        # These tests drive the non-streamed path; the streaming seam is the harness's and is
        # covered where it lives.
        raise NotImplementedError


def _user_texts(model_input) -> list[str]:
    if isinstance(model_input, str):
        return [model_input]
    texts: list[str] = []
    for item in model_input:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.extend(
                part.get("text", "")
                for part in content or []
                if isinstance(part, dict) and part.get("type") in ("input_text", "text")
            )
    return texts


async def _openai_turn(conversation: list, user_text: str) -> tuple[list, str]:
    """One turn built exactly as every OpenAI example builds one: the carried conversation,
    then this message, run through the SDK. Returns the SDK's new input list and the reply."""
    sdk_agent = OpenAIAgent(name="Probe", instructions="", model=_EchoModel())
    result = await Runner.run(
        sdk_agent, input=[*conversation, {"role": "user", "content": user_text}]
    )
    return result.to_input_list(), str(result.final_output)


_OPENAI_EXAMPLES = [
    ("examples.openai_hello.workflow", "OpenAIHelloAgentWorkflow"),
    ("examples.react_agent.workflow", "ReactAgentWorkflow"),
    ("examples.nexus_hello.workflow", "NexusHelloAgentWorkflow"),
    ("examples.sandbox_tools.coding_agent.workflow", "SandboxedCodingAgentWorkflow"),
]


def _agent_class(module: str, name: str) -> type:
    return getattr(importlib.import_module(module), name)


@pytest.mark.parametrize(
    "module,name", _OPENAI_EXAMPLES, ids=[name for _, name in _OPENAI_EXAMPLES]
)
async def test_an_openai_example_still_knows_what_was_said(build, module, name):
    """Say "apples", roll over, say "bananas", and the model has to see both. The SDK's input
    items claim to be plain JSON already, and this is that claim under test: they make the round
    trip through the data converter and go back into the SDK, which is the only thing that
    proves the two-line hook pair is enough."""
    cls = _agent_class(module, name)
    source = build(cls)

    source._conversation, first_reply = await _openai_turn(source._conversation, "apples")
    assert first_reply == "apples"

    target = build(cls)
    await _roll_over(cls, source, target)

    target._conversation, second_reply = await _openai_turn(target._conversation, "bananas")
    assert second_reply == "apples bananas"


@pytest.mark.parametrize(
    "module,name,chosen",
    [
        (
            "examples.monty.conversational_workflow",
            "MontyChatAgentWorkflow",
            "gemini-3.1-flash-lite",
        ),
        (
            "examples.monty.conversational_subagent_workflow",
            "MontyChatSubagentWorkflow",
            "gemini-3.1-flash-lite",
        ),
        (
            "examples.sandbox_tools.coding_agent.workflow",
            "SandboxedCodingAgentWorkflow",
            "gpt-4.1",
        ),
    ],
    ids=["monty-gemini", "monty-gemini-subagent", "sandbox-coding"],
)
async def test_a_model_chosen_with_a_slash_command_survives_the_rollover(
    build, module, name, chosen
):
    """``/model`` is a choice a person made about this session, and a rollover is something they
    never asked for and cannot see. Coming back on the default would be the session quietly
    overruling them, which is why these two snapshots carry the model alongside the transcript.

    The value asserted here is the one the next turn hands the SDK — the ``model`` on the next
    Interactions request, which the chaining test below reads off the request itself.
    """
    module_object = importlib.import_module(module)
    cls = _agent_class(module, name)
    assert chosen in module_object.SUPPORTED_MODELS
    assert chosen != module_object.DEFAULT_MODEL

    source = build(cls)
    source._set_model(chosen)

    target = build(cls)
    assert target._model == module_object.DEFAULT_MODEL
    await _roll_over(cls, source, target)

    assert target._model == chosen


# ---------------------------------------------------------------------------
# Pydantic AI — the one example where the conversation is not plain data
# ---------------------------------------------------------------------------


def _echo_user_prompts(messages: list[ModelMessage], _info: AgentInfo) -> PydanticModelResponse:
    said = [
        str(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    return PydanticModelResponse(parts=[TextPart(" ".join(said))])


async def test_the_pydantic_ai_example_still_knows_what_was_said(build):
    """The example whose hooks do real work: ``self._history`` is Pydantic AI's own model
    objects, so it goes through ``ModelMessagesTypeAdapter`` in both directions. If ``restore``
    handed back the plain dicts the converter delivers, the next ``run`` would reject them —
    the round trip has to rebuild the objects, not merely preserve the bytes."""
    from examples.pydantic_ai_hello.workflow import PydanticAIHelloAgentWorkflow as cls

    model = PydanticAgent(FunctionModel(_echo_user_prompts))
    source = build(cls)

    first = await model.run("apples", message_history=source._history)
    assert first.output == "apples"
    source._history = first.all_messages()

    target = build(cls)
    await _roll_over(cls, source, target)

    second = await model.run("bananas", message_history=target._history)
    assert second.output == "apples bananas"


# ---------------------------------------------------------------------------
# Gemini Interactions examples — the conversation lives on Google's side
# ---------------------------------------------------------------------------


_GEMINI_EXAMPLES = [
    ("examples.monty.conversational_workflow", "MontyChatAgentWorkflow"),
    ("examples.monty.conversational_subagent_workflow", "MontyChatSubagentWorkflow"),
    ("examples.callback_tools.wiki_agent.workflow", "WikiAgentWorkflow"),
    ("examples.callback_tools.coding_agent.workflow", "CodingAgentWorkflow"),
]


@pytest.mark.parametrize(
    "module,name", _GEMINI_EXAMPLES, ids=[name for _, name in _GEMINI_EXAMPLES]
)
async def test_a_gemini_example_keeps_chaining_the_same_conversation(build, module, name):
    """These agents hold no transcript — the Interactions API does, and this end holds the id
    that chains to it. So "the conversation survived" means the next turn continues the same
    server-side conversation instead of opening a fresh one, which is what the request the
    restored agent builds has to show."""
    cls = _agent_class(module, name)
    source = build(cls)
    source._previous_interaction_id = "interactions/before-the-rollover"

    target = build(cls)
    assert target._previous_interaction_id is None
    await _roll_over(cls, source, target)

    chained: dict[str, Any] = {}

    async def record(**kwargs):
        chained.update(kwargs)
        return "ok", [], "interactions/after-the-rollover"

    target._execute_agent_interaction = record
    reply = await target._handle_chat_turn(None, "bananas")

    assert reply == "ok"
    assert chained["previous_interaction_id"] == "interactions/before-the-rollover"
    assert chained["model"] == target._model
    assert target._previous_interaction_id == "interactions/after-the-rollover"


async def test_the_coding_agent_comes_back_knowing_what_it_was_part_way_through(build):
    """Its plan is the one piece of the conversation that really is on this side. A coding
    session is exactly the kind that rolls over, and one that came back having forgotten which
    task it had started would be worse than one that had never planned at all."""
    from examples.callback_tools.coding_agent.tools import TodoItem
    from examples.callback_tools.coding_agent.workflow import CodingAgentWorkflow as cls

    source = build(cls)
    source._todos = [
        TodoItem(content="Read config.py", status="completed"),
        TodoItem(content="Add a test for the parser", status="in_progress"),
        TodoItem(content="Wire the flag through"),
    ]

    target = build(cls)
    await _roll_over(cls, source, target)

    assert [t.content for t in target._todos if t.status == "in_progress"] == [
        "Add a test for the parser"
    ]
    # Rebuilt as TodoItems, not left as the dicts the converter delivers: `todowrite` replaces
    # this list in place with typed items and `todoread` renders `t.status` off them.
    assert all(isinstance(t, TodoItem) for t in target._todos)
    assert [t.content for t in target._todos] == [t.content for t in source._todos]


async def test_the_sandbox_coding_agent_comes_back_knowing_what_it_was_part_way_through(build):
    """Same plan-survival claim as the callback coding agent, for the sandboxed twin."""
    from examples.coding_agent_common.todo_tools import TodoItem
    from examples.sandbox_tools.coding_agent.workflow import (
        SandboxedCodingAgentWorkflow as cls,
    )

    source = build(cls)
    source._todos = [
        TodoItem(content="Read config.py", status="completed"),
        TodoItem(content="Add a test for the parser", status="in_progress"),
        TodoItem(content="Wire the flag through"),
    ]

    target = build(cls)
    await _roll_over(cls, source, target)

    assert [t.content for t in target._todos if t.status == "in_progress"] == [
        "Add a test for the parser"
    ]
    assert all(isinstance(t, TodoItem) for t in target._todos)
    assert [t.content for t in target._todos] == [t.content for t in source._todos]
