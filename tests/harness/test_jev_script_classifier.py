# ABOUTME: Tests for jev_script_classifier: the request it asks Jev about a Code Mode script,
# feature validation, and the jev_classify activity's projection of TypeSafe's answers. The
# classifier's dispatch from a real workflow is covered end to end in test_cedar_approvals.py.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from temporal_agent_harness.harness.agent_protocol import (
    AutoApprovalContext,
    AutoApprovalCriteria,
    AutoApprovalCriteriaSet,
    CodeModeCall,
)
from temporal_agent_harness.harness.code_mode import analyze_script
from temporal_agent_harness.harness.jev_approvals import (
    DEFAULT_JEV_MODEL,
    DEFAULT_SCRIPT_FEATURES,
    ScriptFeature,
    build_classification_request,
    jev_script_classifier,
)

SCRIPT = "for f in ids:\n    book_flight(f)\n"


def _ctx(code_mode: CodeModeCall | None) -> AutoApprovalContext:
    criteria_set = AutoApprovalCriteriaSet(effect="Judged by the Cedar policy.")
    return AutoApprovalContext(
        tool_name="run_travel_code",
        tool_input={"script": SCRIPT},
        inherently_safe=True,
        tool_description="Run a script. Host functions: book_flight(flight_id: str).",
        criteria=AutoApprovalCriteria(
            sets={"cedar": criteria_set}, default="cedar", context={"environment": "test"}
        ),
        criteria_set=criteria_set,
        criteria_set_name="cedar",
        code_mode=code_mode,
    )


def test_request_shows_the_script_its_tools_and_its_static_facts() -> None:
    ctx = _ctx(CodeModeCall(script=SCRIPT, host_functions=frozenset({"book_flight"})))
    facts = analyze_script(SCRIPT, {"book_flight"})
    request = build_classification_request(ctx, facts, DEFAULT_SCRIPT_FEATURES)

    assert request.model == DEFAULT_JEV_MODEL
    assert request.state["script"] == SCRIPT
    assert request.state["tool"] == {
        "name": "run_travel_code",
        "description": "Run a script. Host functions: book_flight(flight_id: str).",
    }
    assert request.state["static_analysis"] == facts.model_dump(mode="json")
    assert request.state["operating_context"] == {"environment": "test"}
    assert "not evidence of what the code does" in request.state["situation"]
    assert set(request.questions) == {f.key for f in DEFAULT_SCRIPT_FEATURES}
    for feature in DEFAULT_SCRIPT_FEATURES:
        assert request.questions[feature.key] == {
            "type": "noul",
            "instructions": feature.question,
            "criteria": {"true": feature.yes, "false": feature.no},
        }


def test_request_refuses_a_call_that_is_not_a_script() -> None:
    with pytest.raises(ValueError, match="not a Code Mode call"):
        build_classification_request(
            _ctx(None), analyze_script("", ()), DEFAULT_SCRIPT_FEATURES
        )


def test_feature_keys_must_be_identifiers_and_distinct() -> None:
    with pytest.raises(ValidationError):
        ScriptFeature(key="not an identifier", question="?", yes="y", no="n")
    feature = ScriptFeature(key="loops", question="?", yes="y", no="n")
    with pytest.raises(ValueError, match="distinct"):
        jev_script_classifier([feature, feature])


async def test_classify_activity_projects_typesafe_answers(monkeypatch) -> None:
    from temporal_agent_harness.harness.jev_approvals import activity as activity_mod

    seen: dict = {}

    class FakeClient:
        async def system_one(self, state, questions, model=None):
            seen.update(state=state, questions=questions, model=model)
            return SimpleNamespace(
                model="jev-1.0",
                request_id="req_7",
                usage=SimpleNamespace(input_tokens=30, output_tokens=4),
                choices={},
                nouls={
                    "data_flows_between_tools": SimpleNamespace(noul=0.82),
                    "unbounded_iteration": SimpleNamespace(noul=0.05),
                },
            )

    monkeypatch.setattr(activity_mod, "_require_jev_extra", lambda: None)
    monkeypatch.setattr(activity_mod, "_typesafe_client", FakeClient)

    ctx = _ctx(CodeModeCall(script=SCRIPT, host_functions=frozenset({"book_flight"})))
    request = build_classification_request(
        ctx, analyze_script(SCRIPT, {"book_flight"}), DEFAULT_SCRIPT_FEATURES[:2]
    )
    answer = await activity_mod.jev_classify(request)

    assert seen["state"] == request.state
    assert seen["questions"] == request.questions
    assert answer.answers == {"data_flows_between_tools": 0.82, "unbounded_iteration": 0.05}
    assert (answer.model, answer.request_id) == ("jev-1.0", "req_7")
    assert (answer.input_tokens, answer.output_tokens) == (30, 4)
