# ABOUTME: Rendering Playpen events as Dogwood trace lines: Cedar literals, escaping, and the
# values that have no Cedar form. No CLI needed.

from __future__ import annotations

import pytest

from temporal_agent_harness.playpen.models import DogwoodEvent
from temporal_agent_harness.playpen.trace import literal, render_event


def test_literals():
    assert literal(True) == "true"
    assert literal(42) == "42"
    assert literal('say "hi"\n\\ ok') == '"say \\"hi\\"\\n\\\\ ok"'
    assert literal("café\x00") == '"caf\\u{e9}\\u{0}"'
    assert literal({"a": 1, "b": None, "c": [1, 2]}) == "{ a: 1, c: [1, 2] }"
    assert literal({}) == "{ }"


@pytest.mark.parametrize("bad", [1.5, 2**63, {"not an id": 1}, object()])
def test_values_with_no_cedar_form_are_rejected(bad):
    with pytest.raises(TypeError):
        literal(bad)


def test_only_a_request_carries_the_request_context():
    request = DogwoodEvent(
        timestamp=7, action="get_order", kind="request", request_id="t1", input={"order_id": "o"}
    )
    response = request.model_copy(update={"kind": "response", "output": {"total_cents": 5}})
    line = render_event("Support", "wf-1", request)
    assert line.startswith('@7 scope(principal: Support::User::"wf-1"')
    assert 'request_context(input: { order_id: "o" })' in line
    assert line.endswith('requestId: "t1")')
    assert "request_context" not in render_event("Support", "wf-1", response)
    assert "output: { total_cents: 5 }" in render_event("Support", "wf-1", response)
