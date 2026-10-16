# ABOUTME: Tests that agent state can hold arbitrary JSON in a `JsonValue` field: nested values
# are frozen at rest, a change inside mutate() is published as an exact patch at its nested path,
# and the snapshot carries the whole value.
#
# Run with: uv run pytest tests/state/test_json_value.py -v

from __future__ import annotations

import pytest
from pydantic import JsonValue

from temporal_agent_harness.harness.state import HarnessState, StateRef
from temporal_agent_harness.harness.state.errors import FrozenError


class Settings(HarnessState):
    name: str = ""
    extra: dict[str, JsonValue] = {}


NESTED = {"auth": {"role": "reader", "scopes": ["a", "b"]}, "limits": [1, {"x": None}]}


def test_a_json_field_holds_nested_values_frozen():
    ref = StateRef("settings", Settings(extra=NESTED))
    assert ref.current.extra == NESTED
    with pytest.raises(FrozenError):
        ref.current.extra["auth"]["role"] = "admin"  # type: ignore[index]


def test_a_nested_change_is_one_exact_patch():
    patches = []
    ref = StateRef("settings", Settings(extra=NESTED), publish=patches.append)
    with ref.mutate() as draft:
        draft.extra["auth"]["scopes"].append("c")  # type: ignore[index, union-attr]
    assert patches[-1].ops == [{"op": "add", "path": "/extra/auth/scopes/-", "value": "c"}]
    assert ref.snapshot().value["extra"]["auth"]["scopes"] == ["a", "b", "c"]
