# ABOUTME: Tests for agent_schema(cls) — an agent's handlers and declared state as one JSON
# Schema document. A golden file pins the tic-tac-toe agent's schema; the rest checks the
# validation/serialization split, $defs naming, and that every example agent produces a
# deterministic schema whose refs all resolve.
#
# Regenerate the golden after an intended change with:
#   uv run temporal-agent-harness schema examples.tictactoe.workflow:TicTacToeAgentWorkflow \
#       -o tests/harness/golden/tictactoe.schema.json

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from temporalio import workflow

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporal_agent_harness.harness.agent_schema import (
    agent_schema,
    dump_agent_schema,
    load_agent_class,
)
from temporal_agent_harness.harness.state import HarnessState

GOLDEN = Path(__file__).parent / "golden" / "tictactoe.schema.json"

EXAMPLE_AGENTS = [
    "examples.auto_mode.workflow:AutoModeTravelAgentWorkflow",
    "examples.callback_tools.coding_agent.workflow:CodingAgentWorkflow",
    "examples.callback_tools.wiki_agent.workflow:WikiAgentWorkflow",
    "examples.monty.conversational_subagent_workflow:MontyChatSubagentWorkflow",
    "examples.monty.conversational_workflow:MontyChatAgentWorkflow",
    "examples.monty.workflow:MontyDynamicAgentWorkflow",
    "examples.nexus_hello.workflow:NexusHelloAgentWorkflow",
    "examples.openai_hello.workflow:OpenAIHelloAgentWorkflow",
    "examples.pydantic_ai_hello.workflow:PydanticAIHelloAgentWorkflow",
    "examples.react_agent.workflow:ReactAgentWorkflow",
    "examples.tictactoe.workflow:TicTacToeAgentWorkflow",
]


def _tictactoe() -> type:
    return load_agent_class("examples.tictactoe.workflow:TicTacToeAgentWorkflow")


def test_tictactoe_matches_the_golden_schema():
    assert dump_agent_schema(_tictactoe()) == GOLDEN.read_text()


def test_handler_inputs_are_validation_mode_and_outputs_and_states_serialization_mode():
    doc = agent_schema(_tictactoe())
    defs = doc["$defs"]

    # Going in, a defaulted field may be omitted...
    assert "required" not in defs["NewGame"]
    # ...coming out, model_dump(mode="json") always carries it.
    assert defs["Board"]["required"] == ["cells", "to_move", "agent_mark", "status", "moves"]
    assert defs["Move"]["required"] == ["mark", "cell"]


def test_the_document_names_the_agent_and_where_it_came_from():
    doc = agent_schema(_tictactoe())
    assert doc["agent"] == "TicTacToeAgent"
    assert doc["source"] == "examples.tictactoe.workflow:TicTacToeAgentWorkflow"
    assert doc["handlers"]["play"] == {
        "description": doc["handlers"]["play"]["description"],
        "mid_turn": "enqueue",
        "input": {"$ref": "#/$defs/PlayMove"},
        "output": {"$ref": "#/$defs/TextReply"},
    }
    assert doc["states"] == {"board": {"$ref": "#/$defs/Board"}}


# ---------------------------------------------------------------------------
# $defs naming
# ---------------------------------------------------------------------------


class Note(HarnessState):
    """A note."""

    text: str = ""


class Echo(BaseModel):
    """Used as both a handler's input and its output."""

    text: str = ""


@workflow.defn(name="EchoAgent")
@agent.defn
class EchoAgent:
    notes = agent.state(Note)

    @workflow.run
    async def run(self, config: AgentConfig) -> None: ...

    @agent.accepts
    async def echo(self, message: Echo) -> Echo:
        """Echo the message back."""
        return message


def test_a_model_used_both_ways_gets_an_input_and_an_output_definition():
    doc = agent_schema(EchoAgent)
    assert doc["handlers"]["echo"]["input"] == {"$ref": "#/$defs/Echo-Input"}
    assert doc["handlers"]["echo"]["output"] == {"$ref": "#/$defs/Echo-Output"}
    assert "required" not in doc["$defs"]["Echo-Input"]
    assert doc["$defs"]["Echo-Output"]["required"] == ["text"]
    assert doc["states"] == {"notes": {"$ref": "#/$defs/Note"}}


def _same_named_model(module: str) -> type[BaseModel]:
    namespace: dict[str, Any] = {"BaseModel": BaseModel, "__name__": module}
    exec(
        "class Payload(BaseModel):\n    '''A payload.'''\n    value: int = 0\n",
        namespace,
    )
    return namespace["Payload"]


FirstPayload = _same_named_model("pkg.first")
SecondPayload = _same_named_model("pkg.second")


@workflow.defn(name="CollidingAgent")
@agent.defn
class CollidingAgent:
    @workflow.run
    async def run(self, config: AgentConfig) -> None: ...

    @agent.accepts
    async def take_first(self, message: FirstPayload) -> Echo:  # type: ignore[valid-type]
        """Take the first kind of payload."""
        raise NotImplementedError

    @agent.accepts
    async def take_second(self, message: SecondPayload) -> Echo:  # type: ignore[valid-type]
        """Take the second kind of payload."""
        raise NotImplementedError


@workflow.defn(name="CrossModeCollidingAgent")
@agent.defn
class CrossModeCollidingAgent:
    @workflow.run
    async def run(self, config: AgentConfig) -> None: ...

    @agent.accepts
    async def swap(self, message: FirstPayload) -> SecondPayload:  # type: ignore[valid-type]
        """Take one payload and return the other."""
        raise NotImplementedError


@pytest.mark.parametrize("cls", [CollidingAgent, CrossModeCollidingAgent])
def test_two_models_with_one_class_name_fail_naming_both(cls: type):
    with pytest.raises(ValueError, match=r"pkg.first.*Payload.*pkg.second.*Payload"):
        agent_schema(cls)


# ---------------------------------------------------------------------------
# Every example agent
# ---------------------------------------------------------------------------


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return found + [r for value in node.values() for r in _refs(value)]
    if isinstance(node, list):
        return [r for item in node for r in _refs(item)]
    return []


@pytest.mark.parametrize("target", EXAMPLE_AGENTS)
def test_every_example_agent_has_a_deterministic_self_contained_schema(
    target: str, monkeypatch: pytest.MonkeyPatch
):
    # The Pydantic AI example builds its model provider at import time.
    monkeypatch.setenv("OPENAI_API_KEY", "unused")
    cls = load_agent_class(target)

    text = dump_agent_schema(cls)
    assert dump_agent_schema(cls) == text

    doc = json.loads(text)
    assert doc["handlers"], "every example agent accepts at least one message"
    for ref in _refs(doc):
        assert ref.startswith("#/$defs/")
        assert ref.removeprefix("#/$defs/") in doc["$defs"], ref


def test_load_agent_class_explains_a_bad_target():
    with pytest.raises(ValueError, match="expected module.path:ClassName"):
        load_agent_class("examples.tictactoe.workflow")
    with pytest.raises(ValueError, match="no module named 'examples.nope'"):
        load_agent_class("examples.nope:Agent")
    with pytest.raises(ValueError, match="has no attribute 'Nope'"):
        load_agent_class("examples.tictactoe.workflow:Nope")
    with pytest.raises(ValueError, match="is not a @workflow.defn agent class"):
        load_agent_class("examples.tictactoe.board:Board")

