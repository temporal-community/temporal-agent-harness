# ABOUTME: Tests the authoring-time contract on @agent.snapshot / @agent.restore — the pair an
# agent declares so its conversation can cross a continue-as-new.
#
# All offline, and all about the same thing: every way of getting the pair wrong should be caught
# where the author is still looking rather than at the first rollover, which is hours into a
# conversation and the hardest moment to reproduce. A malformed pair fails at import, which for a
# worker means at startup; a snapshot that is not JSON-native fails when it is taken, since that
# is the earliest anything can know. (The third way — a supplied stream that switches rollover off
# despite a perfectly good pair — needs a live workflow to observe, so it lives in
# tests/harness/test_continue_as_new.py alongside the veto it defeats.)
#
# Run with: uv run pytest tests/harness/test_resumption_hooks.py -v

from __future__ import annotations

from typing import Any

import pytest
from temporalio import workflow

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import (
    AgentWorkflowRunner,
    ResumptionHooks,
    _assert_json_native_snapshot,
    agent_resumption_hooks,
)


class _AgentBody:
    """Everything an agent needs to be valid, minus the pair under test."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config, approval_policy_default=ToolApprovalPolicy.dangerously_skip_all()
        )
        self._conversation: list[str] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Say something back."""
        return TextReply(text=message.text)


def test_a_declared_pair_is_found_and_usable():
    @agent.defn
    class Agent(_AgentBody):
        @agent.snapshot
        def snapshot(self) -> dict[str, Any]:
            return {"conversation": self._conversation}

        @agent.restore
        def restore(self, state: dict[str, Any]) -> None:
            self._conversation = list(state["conversation"])

    hooks: ResumptionHooks | None = agent_resumption_hooks(Agent)
    assert hooks is not None

    # Called the way the runner calls them: unbound, against the agent instance it holds.
    instance = Agent.__new__(Agent)
    instance._conversation = ["apples"]
    hooks.restore(instance, hooks.snapshot(instance))
    assert instance._conversation == ["apples"]


def test_an_agent_that_declares_neither_is_simply_not_resumable():
    """Not an error. It is the documented way to opt out, and the default for anything that
    predates the hooks — such an agent works exactly as it always did and never rolls over."""

    @agent.defn
    class Agent(_AgentBody):
        pass

    assert agent_resumption_hooks(Agent) is None


@pytest.mark.parametrize("declared", ["snapshot", "restore"])
def test_half_a_pair_is_rejected(declared):
    """Half of it carries nothing: a snapshot nobody restores, or a restore with nothing to
    hand it. The one thing worse than not rolling over is rolling over empty."""
    with pytest.raises(TypeError, match="exactly one"):

        @agent.defn
        class Agent(_AgentBody):
            if declared == "snapshot":

                @agent.snapshot
                def snapshot(self) -> dict[str, Any]:
                    return {}

            else:

                @agent.restore
                def restore(self, state: dict[str, Any]) -> None:
                    pass


def test_two_snapshots_are_rejected():
    with pytest.raises(TypeError, match="2 @agent.snapshot"):

        @agent.defn
        class Agent(_AgentBody):
            @agent.snapshot
            def snapshot(self) -> dict[str, Any]:
                return {}

            @agent.snapshot
            def also_snapshot(self) -> dict[str, Any]:
                return {}

            @agent.restore
            def restore(self, state: dict[str, Any]) -> None:
                pass


def test_an_async_hook_is_rejected():
    """The hooks run at the moment the runner has established that nothing is in flight. An
    await there would reopen the window that the choice of rollover point exists to close."""
    with pytest.raises(TypeError, match="must be synchronous"):

        @agent.defn
        class Agent(_AgentBody):
            @agent.snapshot
            async def snapshot(self) -> dict[str, Any]:
                return {}

            @agent.restore
            def restore(self, state: dict[str, Any]) -> None:
                pass


def test_a_restore_that_takes_no_state_is_rejected():
    with pytest.raises(TypeError, match=r"must take \(self, state\)"):

        @agent.defn
        class Agent(_AgentBody):
            @agent.snapshot
            def snapshot(self) -> dict[str, Any]:
                return {}

            @agent.restore
            def restore(self) -> None:
                pass


# ---------------------------------------------------------------------------
# The snapshot has to be JSON-native, and nothing else would notice
# ---------------------------------------------------------------------------
#
# This is the one part of the contract a careful author still gets wrong, because every check
# available to them passes. ``restore(snapshot(agent))`` in one process hands the real objects
# straight back, and the pydantic data converter under continue-as-new encodes a model perfectly
# happily — so the mistake surfaces one run later, as the successor's agent quietly holding dicts
# where it expects objects.


class _NotJson:
    """Stands in for the SDK object an author forgot to convert."""


def test_a_plain_json_snapshot_is_accepted():
    state = {"conversation": [{"role": "user", "content": "apples"}], "model": None}
    assert _assert_json_native_snapshot("Agent", state) is state


def test_a_snapshot_holding_an_object_names_the_key_and_the_type():
    """The error has to be actionable at a glance: which key, and what is in it. A generic
    "not serializable" would leave the author bisecting their own conversation."""
    with pytest.raises(TypeError) as raised:
        _assert_json_native_snapshot("Agent", {"todos": [{"item": _NotJson()}]})

    message = str(raised.value)
    assert "Agent's @agent.snapshot" in message
    assert "state['todos'][0]['item'] is a _NotJson" in message
    # And what to do about it, since the fix is not obvious from the symptom.
    assert "model_dump" in message


def test_a_snapshot_that_is_not_a_dict_is_rejected_as_such():
    """Returning the conversation instead of a dict keyed by it would otherwise fail as a
    validation error about AgentResumeState, which names the harness rather than the hook."""
    with pytest.raises(TypeError, match="returned a list, not a dict"):
        _assert_json_native_snapshot("Agent", ["apples"])


def test_something_json_cannot_key_is_reported_as_a_key():
    with pytest.raises(TypeError, match=r"a key of state\['conversation'\] is a tuple"):
        _assert_json_native_snapshot("Agent", {"conversation": {(1, 2): "apples"}})
