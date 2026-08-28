# ABOUTME: ``run_script`` — start a fresh agent session, replay a TurnScript against it, and
# return every turn. The execution half of an eval; scoring is a separate concern (scoring.py).
#
# Deliberately simple: one session per case, run in-process. The durable fan-out version (a
# dataset run as its own Temporal workflow, so a half-finished 5,000-item run resumes without
# re-spending tokens) is a later phase and builds on exactly this function.

from __future__ import annotations

import uuid
from typing import Any

from temporalio.client import Client

from temporal_agent_harness.evals.script import ScriptResult, TurnScript
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig

DEFAULT_SESSION_PREFIX = "eval"


async def run_script(
    client: Client,
    script: TurnScript,
    *,
    session_id: str | None = None,
    labels: dict[str, str] | None = None,
    timeout: float | None = None,
    close_session: bool = True,
) -> ScriptResult:
    """Run one eval case: start an agent, replay every step, return all the turns.

    Each case gets its OWN freshly-started session, so cases cannot contaminate each other
    through conversation history — the agent equivalent of a fresh fixture per test.

    ``labels`` are merged into the session's ``AgentConfig.labels``, which is how a run tags
    itself (``dataset_item_id``, ``experiment``, …) so the resulting traces are self-describing
    without the caller having to correlate anything afterwards.

    A step that fails does NOT raise: the turn is recorded with its error and the script stops
    there, with :attr:`ScriptResult.ok` False. A failed case is a result to be scored, not an
    exception to be handled — and stopping is right because the remaining steps of a
    conversation are meaningless once one turn has gone wrong.

    ``close_session`` sends the harness ``close`` signal when the script finishes. Leave it on:
    an agent session is a long-lived workflow that parks awaiting the next message, so a
    5,000-item dataset would otherwise leave 5,000 workflows running forever.
    """
    session_id = session_id or f"{DEFAULT_SESSION_PREFIX}-{uuid.uuid4()}"
    config = script.config.model_copy(
        update={"labels": {**(script.config.labels or {}), **(labels or {})}}
    )
    handle = await client.start_workflow(
        script.workflow_type,
        config,
        id=session_id,
        task_queue=script.task_queue,
    )
    agent_client = AgentClient(client, handle.id)
    result = ScriptResult(session_workflow_id=handle.id)

    try:
        for index, step in enumerate(script.steps):
            turn = await agent_client.run_turn(
                step.function,
                step.payload,
                labels={"step_index": str(index), **step.labels},
                timeout=timeout,
                # The eval wants a failed turn as data, and wants the events either way —
                # a case that failed is often the most interesting one to look at.
                raise_on_error=False,
            )
            result.turns.append(turn)
            if not turn.ok:
                break
    except Exception as e:  # noqa: BLE001 — an infrastructure failure is a case result too
        # Distinct from a turn error: this is the harness/Temporal itself failing (timeout,
        # worker gone, stale turn). Recorded rather than raised so one broken case cannot take
        # down a whole dataset run.
        result.error = f"{type(e).__name__}: {e}"
    finally:
        if close_session:
            await _close_quietly(handle)

    return result


async def _close_quietly(handle: Any) -> None:
    """Signal ``close``, swallowing failures.

    Best-effort on purpose: the run already produced its result, and a cleanup failure (the
    workflow already finished, a transient RPC error) must not turn a scored case into an
    exception. A leaked session is a cost problem; a lost result is a correctness problem.
    """
    try:
        await handle.signal("close")
    except Exception:  # noqa: BLE001,S110 — see docstring
        pass


__all__ = ["DEFAULT_SESSION_PREFIX", "run_script"]
