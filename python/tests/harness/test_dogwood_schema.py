# ABOUTME: Tests for dogwood_schema, which writes the Dogwood action schema for the tool calls
# dogwood_evaluator sends and the input events it records: the declared shapes, and that what
# Dogwood can't see is left out with a comment. Pure text checks; no Dogwood engine.

from __future__ import annotations

from typing import Literal, Optional

import pytest
from pydantic import BaseModel

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import AgentConfig, TextMessage, TextReply
from temporal_agent_harness.harness.dogwood_approvals import dogwood_schema


class Address(BaseModel):
    city: str
    zip_code: Optional[str] = None


class Node(BaseModel):
    name: str
    child: Optional["Node"] = None


class Order(BaseModel):
    order_id: str
    quantity: int
    express: bool
    price: float
    tags: list[str]
    status: Literal["open", "closed"]
    ship_to: Address
    notes: dict[str, str]


@agent.tool_defn()
async def place_order(order: Order, priority: int = 0) -> str:
    """Place an order."""
    return "ok"


@agent.tool_defn()
async def walk(tree: Node) -> str:
    """Walk a tree."""
    return "ok"


class Receipt(BaseModel):
    confirmation: str
    total: float


@agent.tool_defn()
async def pay(amount: int) -> Receipt:
    """Pay."""
    return Receipt(confirmation="c", total=1.0)


@agent.tool_defn()
async def run_sql(sql: str) -> str:
    """Run a query."""
    return "rows"


class QueryFacts(BaseModel):
    statement: str
    table_functions: list[str]


class TableRead(BaseModel):
    table: str


@agent.defn
class SchemaProbeAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(config)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat."""
        return TextReply(text=message.text)


def _action(schema: str, name: str) -> str:
    start = schema.index(f'action "{name}"')
    return schema[start : schema.index("};", start)]


def test_ordinary_tool_arguments() -> None:
    action = _action(dogwood_schema([place_order], action_prefix="p"), "p/place_order")
    for line in [
        "order?: {",
        "order_id?: String,",
        "quantity?: Long,",
        "express?: Bool,",
        "tags?: Set<String>,",
        "status?: String,",
        "ship_to?: {",
        "city?: String,",
        "zip_code?: String,",
        "priority?: Long,",
        "// price: a float, which Dogwood can't see",
        "// notes: a map, which Dogwood can't see",
    ]:
        assert line in action, line


def test_a_model_that_contains_itself_stops_at_the_cycle() -> None:
    action = _action(dogwood_schema([walk], action_prefix="p"), "p/walk")
    assert "tree?: {" in action
    assert "name?: String," in action
    assert "// child:" in action


def test_call_inputs_replace_a_tools_arguments_and_declare_checks() -> None:
    schema = dogwood_schema(
        [run_sql],
        action_prefix="p",
        call_inputs={"run_sql": QueryFacts, "read_table": TableRead},
    )
    call = _action(schema, "p/run_sql")
    assert "statement?: String," in call
    assert "table_functions?: Set<String>," in call
    assert "sql?:" not in call
    # The tool's result keeps its own action.
    assert "arg0?: String," in _action(schema, "p/run_sql/result")
    # A check is an action of its own, with no result.
    assert "table?: String," in _action(schema, "p/read_table")
    assert 'action "p/read_table/result"' not in schema


def test_refuses_a_plain_function() -> None:
    async def not_a_tool() -> str:
        return ""

    with pytest.raises(TypeError, match="not a harness tool"):
        dogwood_schema([not_a_tool], action_prefix="p")


def test_actions_for_tool_results_and_messages() -> None:
    schema = dogwood_schema([place_order, pay], action_prefix="p", agent_class=SchemaProbeAgent)
    assert schema.startswith("// Generated") and "namespace Temporal {" in schema
    paid = _action(schema, "p/pay/result")
    assert "arg0?: {" in paid
    assert "confirmation?: String," in paid
    assert "// total: a float, which Dogwood can't see" in paid
    # A result that isn't an object is the value itself.
    assert "arg0?: String," in _action(schema, "p/place_order/result")
    # Beside each result, the call's arguments.
    assert "arg1?: {" in paid and "amount?: Long," in paid
    message = _action(schema, "update/ask")
    assert "text?: String," in message
    for action in (paid, message, _action(schema, "p/pay")):
        assert "principal: [Worker], resource: [TaskQueue]," in action
        assert "parameterCount: Long," in action
        assert "caller: Caller," in action


def test_without_an_agent_class_no_messages_are_declared() -> None:
    assert 'action "update/' not in dogwood_schema([pay], action_prefix="p")
