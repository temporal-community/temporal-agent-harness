# ABOUTME: Tests for cedar_schema, which writes the Cedar schema for the tool calls
# cedar_evaluator sends: the declared shapes, and that what Cedar can't see is left out with a
# comment. Pure text checks; no Cedar engine.

from __future__ import annotations

from typing import Literal, Optional

import pytest
from pydantic import BaseModel

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.cedar_approvals.schema import cedar_schema
from temporal_agent_harness.harness.jev_approvals import ScriptFeature

pytest.importorskip("pydantic_monty")


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


def _action(schema: str, name: str) -> str:
    start = schema.index(f'action "{name}"')
    return schema[start : schema.index("};", start)]


def test_ordinary_tool_arguments() -> None:
    action = _action(cedar_schema([place_order], action_prefix="p"), "p/place_order")
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
        "// price: a float, which Cedar can't see",
        "// notes: a map, which Cedar can't see",
    ]:
        assert line in action, line


def test_a_model_that_contains_itself_stops_at_the_cycle() -> None:
    action = _action(cedar_schema([walk], action_prefix="p"), "p/walk")
    assert "tree?: {" in action
    assert "name?: String," in action
    assert "// child:" in action


def test_code_mode_tool_declares_its_host_functions_and_features() -> None:
    code_tool = agent.code_mode_tool([place_order, walk], name="run_orders")
    features = [ScriptFeature(key="unbounded_iteration", question="?", yes="y", no="n")]
    action = _action(
        cedar_schema([code_tool], action_prefix="p", script_features=features), "p/run_orders"
    )
    use = "{ call_sites: Long, in_loop: Bool, concurrent: Bool, indirect: Bool }"
    for line in [
        "calls?: Set<String>,",
        f"place_order?: {use},",
        f"walk?: {use},",
        "has_while_loop?: Bool,",
        "dynamic_code?: Bool,",
        "classification?: {",
        "unbounded_iteration?: Long,",
    ]:
        assert line in action, line


def test_refuses_a_plain_function() -> None:
    async def not_a_tool() -> str:
        return ""

    with pytest.raises(TypeError, match="not a harness tool"):
        cedar_schema([not_a_tool], action_prefix="p")
