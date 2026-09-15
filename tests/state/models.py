# ABOUTME: Representative HarnessState schemas shared by the state-layer tests —
# nesting, lists of models, dicts of lists, tuples and enums in one place. No sets: the schema
# checker rejects them (see test_schema), so a fixture carrying one would not define.

"""Representative state schemas shared by the test modules."""

from __future__ import annotations

import enum
from typing import Literal

from temporal_agent_harness.harness.state import HarnessState


class Priority(enum.StrEnum):
    LOW = "low"
    HIGH = "high"


class Todo(HarnessState):
    id: str
    text: str = ""
    done: bool = False
    tags: list[str] = []
    priority: Priority = Priority.LOW


class Group(HarnessState):
    name: str
    todos: list[Todo] = []
    meta: dict[str, str] = {}


class AgentState(HarnessState):
    """The kitchen sink: nesting, lists of models, dicts of lists, sets, tuples."""

    model: str = "claude-sonnet-5"
    count: int = 0
    ratio: float | None = None
    kind: Literal["a", "b"] = "a"
    groups: list[Group] = []
    index: dict[str, list[Todo]] = {}
    scores: dict[int, int] = {}
    tags: list[str] = []
    pair: tuple[int, str] = (0, "")
    matrix: tuple[list[int], ...] = ()


class Flat(HarnessState):
    a: int = 0
    b: str = ""
    c: bool = False


class Inner(HarnessState):
    value: int = 0


class Outer(HarnessState):
    inner: Inner = Inner()
    name: str = ""
