# ABOUTME: Unit tests for the subagent wrapper's pure, in-workflow bookkeeping — the
# per-subagent FIFO gate (call-order admission) and the subagent registry on _WorkflowStatus.
# These need no Temporal env; the activity dispatch + end-to-end parent→subagent flow are
# covered separately (they require a running child agent workflow).

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel, Field
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import (
    AgentEvent,
    SlashCommand,
    SubagentMessageSent,
    SubagentStarted,
    SubagentStopped,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import _SubagentInstance, _WorkflowStatus


# A minimal child agent for exercising the toolset generator. No @workflow.defn is needed —
# subagent_toolset reads the @agent.accepts handlers by pure reflection, and falls back to the
# class name for the workflow type. ``ask`` and ``summarize`` deliberately share one input model
# (name-routed dispatch allows it).
class _Question(BaseModel):
    """A question to research."""

    text: str = Field(description="The natural-language question to research.")


class _Answer(BaseModel):
    """An answer to a question."""

    text: str = Field(description="The answer text.")


class _SampleChildAgent:
    @agent.accepts
    async def ask(self, q: _Question) -> _Answer:
        """Answer a free-form question."""
        ...

    @agent.accepts
    async def summarize(self, q: _Question) -> _Answer:
        """Summarize the conversation so far."""
        ...


class _SampleChildAgentWithSlash:
    @agent.accepts
    async def ask(self, q: _Question) -> _Answer:
        """Answer a free-form question."""
        ...

    @agent.accepts
    async def slash(self, command: SlashCommand) -> TextReply:
        """Handle operator slash commands."""
        ...


def _status() -> _WorkflowStatus:
    return _WorkflowStatus(
        agent_id="parent",
        is_message_queuing_enabled=False,
        approval_policy=ToolApprovalPolicy.allow_inherently_safe(),
    )


def test_gate_hands_out_tickets_in_call_order():
    inst = _SubagentInstance(handle="h", workflow_id="wf", agent_key="k")
    # Tickets are monotonic so gathered callers are ordered by the order they call take_ticket
    # (i.e. the model's call order), not by await scheduling.
    assert [inst.take_ticket() for _ in range(3)] == [0, 1, 2]


def test_gate_serves_one_ticket_at_a_time_in_order():
    inst = _SubagentInstance(handle="h", workflow_id="wf", agent_key="k")
    t0, t1, t2 = inst.take_ticket(), inst.take_ticket(), inst.take_ticket()

    # Only the first ticket is admitted initially.
    assert inst.is_serving(t0)
    assert not inst.is_serving(t1)
    assert not inst.is_serving(t2)

    # Releasing passes the gate to the next ticket, strictly in order.
    inst.release_gate()
    assert not inst.is_serving(t0)
    assert inst.is_serving(t1)
    assert not inst.is_serving(t2)

    inst.release_gate()
    assert inst.is_serving(t2)


def test_gate_sequential_take_serve_release_never_blocks():
    inst = _SubagentInstance(handle="h", workflow_id="wf", agent_key="k")
    # The common (non-concurrent) path: take → already serving → release, repeatedly. Each
    # ticket is served the moment it is taken (no waiting), since the prior one released.
    for expected in range(3):
        ticket = inst.take_ticket()
        assert ticket == expected
        assert inst.is_serving(ticket)
        inst.release_gate()
    # Three releases advanced _serving to 3, so the next ticket (3) is served immediately too.
    assert inst.take_ticket() == 3
    assert inst.is_serving(3)


def test_register_keys_by_handle_and_stores_workflow_id():
    st = _status()
    inst = st.register_subagent("a3f9c2", "sample-subagent-<uuid>", "sample")
    assert inst.handle == "a3f9c2"
    assert inst.workflow_id == "sample-subagent-<uuid>"  # the real child id, hidden from the model
    assert inst.agent_key == "sample"
    assert inst.next_expected_turn == 1
    assert inst.last_consumed_offset == 0
    assert st.subagent("a3f9c2") is inst
    assert st.has_subagent("a3f9c2") and not st.has_subagent("nope")


def test_lookup_unknown_subagent_raises_typed_error():
    st = _status()
    with pytest.raises(ApplicationError) as excinfo:
        st.subagent("nope")
    err = excinfo.value
    assert err.type == "UnknownSubagent"
    assert err.non_retryable
    # details carry the offending handle + the known set for a useful model-facing message.
    assert err.details[0] == {"handle": "nope", "known": []}


def test_remove_subagent_is_idempotent_and_then_unknown():
    st = _status()
    st.register_subagent("a3f9c2", "wf", "sample")
    st.remove_subagent("a3f9c2")
    with pytest.raises(ApplicationError):
        st.subagent("a3f9c2")
    # Removing again is a no-op, not an error.
    st.remove_subagent("a3f9c2")


def test_agent_status_lists_subagents_without_gate_internals():
    st = _status()
    inst = st.register_subagent("a3f9c2", "sample-subagent-wf", "sample")
    inst.next_expected_turn = 4
    # Hand out a couple of gate tickets so the internal counters are non-default.
    inst.take_ticket()
    inst.take_ticket()

    status = st.to_agent_status()
    assert len(status.subagents) == 1
    info = status.subagents[0]
    assert (info.subagent_id, info.agent_key, info.workflow_id, info.next_expected_turn) == (
        "a3f9c2",
        "sample",
        "sample-subagent-wf",
        4,
    )
    # The caller-side gate's ticket counters are an implementation detail and must NOT leak
    # into the status projection.
    fields = set(vars(info))
    assert "_next_ticket" not in fields
    assert "_serving" not in fields
    assert fields == {"subagent_id", "agent_key", "workflow_id", "next_expected_turn"}


def test_subagent_lifecycle_events_carry_workflow_id_and_round_trip():
    # SubagentStarted/Stopped must survive the AgentEvent discriminated-union round-trip with
    # the child workflow_id intact — that field is what lets a consumer dynamically mount the
    # subagent's own stream for a consolidated view.
    for ev in (
        SubagentStarted(subagent_id="a3f9c2", agent_key="sample", workflow_id="sample-subagent-wf"),
        SubagentStopped(subagent_id="a3f9c2", agent_key="sample", workflow_id="sample-subagent-wf"),
    ):
        envelope = AgentEvent(
            event=ev, agent_id="parent-wf", turn_id="t1", turn_number=2, timestamp=0.0
        )
        back = AgentEvent.model_validate_json(envelope.model_dump_json())
        assert type(back.event) is type(ev)
        # The envelope agent_id is the PARENT; the payload subagent_id is the child being driven.
        assert back.agent_id == "parent-wf"
        assert back.event.workflow_id == "sample-subagent-wf"
        assert back.event.subagent_id == "a3f9c2"
        assert back.event.agent_key == "sample"


def test_subagent_message_sent_event_round_trips_with_dispatch_details():
    # SubagentMessageSent marks a dispatch to a specific subagent on the parent's stream. It
    # must round-trip through the discriminated union carrying enough to correlate with the
    # child's own stream: the handle/workflow_id, the target handler function, and the child
    # turn number.
    ev = SubagentMessageSent(
        subagent_id="a3f9c2",
        agent_key="monty",
        workflow_id="monty-subagent-wf",
        function="run_script",
        subagent_turn=3,
    )
    # Envelope turn_number (the parent's turn) is deliberately DIFFERENT from subagent_turn (the
    # child's turn) — the two must not be conflated, which is exactly why the payload field is
    # named subagent_turn rather than turn_number.
    envelope = AgentEvent(
        event=ev, agent_id="parent-wf", turn_id="t1", turn_number=5, timestamp=0.0
    )
    back = AgentEvent.model_validate_json(envelope.model_dump_json())
    assert type(back.event) is SubagentMessageSent
    assert back.event.subagent_id == "a3f9c2"
    assert back.event.agent_key == "monty"
    assert back.event.workflow_id == "monty-subagent-wf"
    assert back.event.function == "run_script"
    assert back.event.subagent_turn == 3
    assert back.turn_number == 5  # the parent turn, on the envelope — not the subagent's


def test_toolset_emits_namespaced_start_send_stop_tools():
    tools = agent.subagent_toolset(_SampleChildAgent, key="sample", task_queue="sample-q")
    # start_<key>, one <key>_<fn> per handler (discovery order is alphabetical), then stop_<key>.
    assert [t.__name__ for t in tools] == ["start_sample", "sample_ask", "sample_summarize", "stop_sample"]
    # Every generated tool is a real inline tool_defn — so it inherits run_tool dispatch, the
    # approval gate, and tool lifecycle events with no extra wiring.
    assert all(getattr(t, "__agent_tool__", False) for t in tools)


def test_toolset_hides_operator_slash_handler():
    tools = agent.subagent_toolset(
        _SampleChildAgentWithSlash, key="sample", task_queue="sample-q"
    )

    assert [t.__name__ for t in tools] == ["start_sample", "sample_ask", "stop_sample"]


def test_send_tool_signature_uses_the_childs_real_models():
    tools = {t.__name__: t for t in agent.subagent_toolset(
        _SampleChildAgent, key="sample", task_queue="sample-q"
    )}
    ask = tools["sample_ask"]
    sig = inspect.signature(ask)
    # subagent handle (str) + the handler's own input param name, typed as the real input model.
    assert list(sig.parameters) == ["subagent", "q"]
    assert sig.parameters["subagent"].annotation is str
    assert sig.parameters["q"].annotation is _Question
    assert sig.return_annotation is _Answer
    # The model-facing annotations (what function_param/from_callable introspects) match — so the
    # emitted schema is the child's real input model (nested, descriptions preserved) and the
    # return type is the real output model.
    assert ask.__annotations__ == {"subagent": str, "q": _Question, "return": _Answer}


def test_send_tool_docstring_carries_the_handler_description():
    tools = {t.__name__: t for t in agent.subagent_toolset(
        _SampleChildAgent, key="sample", task_queue="sample-q"
    )}
    assert "Answer a free-form question." in (tools["sample_ask"].__doc__ or "")
    assert "Summarize the conversation so far." in (tools["sample_summarize"].__doc__ or "")


def test_start_and_stop_tool_signatures():
    tools = {t.__name__: t for t in agent.subagent_toolset(
        _SampleChildAgent, key="sample", task_queue="sample-q"
    )}
    assert list(inspect.signature(tools["start_sample"]).parameters) == []
    assert inspect.signature(tools["start_sample"]).return_annotation is str
    assert list(inspect.signature(tools["stop_sample"]).parameters) == ["subagent"]


def test_toolset_requires_at_least_one_handler():
    class _NoHandlers:
        pass

    with pytest.raises(TypeError, match="no @agent.accepts handlers"):
        agent.subagent_toolset(_NoHandlers, key="x", task_queue="q")


def test_distinct_subagents_have_independent_gates_and_counters():
    st = _status()
    a = st.register_subagent("aaa111", "wf-a", "sample")
    b = st.register_subagent("bbb222", "wf-b", "sample")
    # Same agent_key, but independent instances/gates so different subagents run concurrently.
    assert a is not b
    a.take_ticket()
    assert a._next_ticket == 1
    assert b._next_ticket == 0
