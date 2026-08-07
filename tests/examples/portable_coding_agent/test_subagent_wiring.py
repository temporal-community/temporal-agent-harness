"""Static check that the `task` subagent toolset wires up (no workflow started)."""

from temporal_agent_harness.harness.subagent_toolset import subagent_toolset

from examples.portable_coding_agent.workflow import PortableCodingAgentWorkflow


def test_task_toolset_generates_start_send_stop():
    tools = subagent_toolset(
        PortableCodingAgentWorkflow, key="task", task_queue="portable-coding-agent"
    )
    names = {t.__name__ for t in tools}
    # start_<key>, one send tool per @agent.accepts handler (ask), and stop_<key>.
    assert "start_task" in names
    assert "stop_task" in names
    assert any("ask" in n for n in names)
