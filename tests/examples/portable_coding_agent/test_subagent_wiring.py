"""Static check that the `task` subagent toolset wires up (no workflow started)."""

from temporal_agent_harness.harness.subagent_toolset import subagent_toolset

from examples.portable_coding_agent.workflow import (
    PortableCodingAgentWorkflow,
    _subagent_policy,
)


def test_task_toolset_generates_start_send_stop():
    tools = subagent_toolset(
        PortableCodingAgentWorkflow, key="task", task_queue="portable-coding-agent"
    )
    names = {t.__name__ for t in tools}
    # start_<key>, one send tool per @agent.accepts handler (ask), and stop_<key>.
    assert "start_task" in names
    assert "stop_task" in names
    assert any("ask" in n for n in names)


def test_subagent_policy_gates_ask_and_delegation_by_depth():
    # Top-level (no id, or a single-segment id): can ask the user and can delegate.
    assert _subagent_policy(None) == (True, True)
    assert _subagent_policy("abcdef") == (True, True)
    # A subagent (compound id) cannot ask the user, and at the depth cap cannot delegate further.
    assert _subagent_policy("abcdef-123456") == (False, False)
    assert _subagent_policy("abcdef-123456-789abc") == (False, False)
