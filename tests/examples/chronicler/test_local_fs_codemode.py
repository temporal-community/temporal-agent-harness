# ABOUTME: Guards the integration claim that the Chronicler's local CALLBACK tools compose with
# Code Mode — agent.code_mode_tool accepts them alongside the stateless Gemini compute activities
# and advertises them all as host functions. If callback tools ever stopped carrying the
# harness-tool marker Code Mode requires, this fails at construction (no Temporal needed).
#
# Run with: uv run pytest tests/examples/chronicler/test_local_fs_codemode.py -v

from __future__ import annotations

from temporal_agent_harness.harness import agent

from examples.chronicler import chronicler_activities as tools
from examples.chronicler.local_fs_tools import LOCAL_TOOLS


def test_callback_tools_carry_harness_tool_marker() -> None:
    # This marker is what lets a callback tool pass code_mode_tool's _validate_tools.
    for tool in LOCAL_TOOLS:
        assert getattr(tool, "__agent_tool__", False), tool.__name__


def test_code_mode_accepts_callbacks_beside_compute_activities() -> None:
    code_tool = agent.code_mode_tool(
        [
            tools.generate_sample_audio_activity,
            tools.transcribe_recording_activity,
            tools.summarize_transcript_activity,
            tools.extract_entities_activity,
            tools.synthesize_audio_activity,
            tools.notify_activity,
            *LOCAL_TOOLS,
        ],
        name="run_chronicler_code",
    )
    interface = code_tool.__doc__ or ""
    # Both the durable compute tools and every callback tool show up as host functions.
    assert "transcribe_recording" in interface
    for tool in LOCAL_TOOLS:
        assert tool.__name__ in interface


def test_chronicler_workflow_builds_its_code_tool() -> None:
    # Exercises that the workflow module (which references LOCAL_TOOLS in its code_mode_tool list)
    # imports cleanly through the sandbox guard and wires the callbacks into the actual agent.
    import examples.chronicler.conversational_workflow as wf

    assert wf.ChroniclerAgentWorkflow is not None
    assert wf.LOCAL_TOOLS is LOCAL_TOOLS
