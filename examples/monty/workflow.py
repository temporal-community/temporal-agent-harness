"""Monty dynamic-workflow agent: each turn runs a sandboxed Python script.

This is a deliberately minimal harness agent. It exists to explore one idea: let a
turn carry an arbitrary Python *script* (see :class:`RunScript`) and execute it inside
the `pydantic-monty <https://pypi.org/project/pydantic-monty/>`_ sandbox, where the
only escape hatches are **host functions** the workflow injects — and each host
function is backed by a durable Temporal activity.

The payoff of running the sandbox *in the workflow* (rather than in an activity) is
exactly that: when the script calls a host function, the workflow turns it into a
durable ``workflow.execute_activity`` call. The dynamically supplied script gets to
orchestrate durable work without itself being trusted with Temporal (or anything else).

Contrast with the conversational Monty agents (``conversational_workflow.py``): those drive
the Gemini Interactions API with a model in the loop. This one has no model in the loop at
all — the "plan" arrives pre-written as the script. It reuses the same harness contract:
``@agent.defn`` + an :class:`AgentWorkflowRunner` built in ``@workflow.init``, the turn
loop driven by ``await runner.run(self)``, and a single ``@agent.accepts`` handler
(``run_script``) whose return value becomes the turn's reply.

Caveat (this is an experiment): the Monty interpreter is a compiled extension; we pass
it through the Temporal sandbox via ``imports_passed_through``. Whether its async
runtime cooperates cleanly with the workflow event loop across activity ``await``s is
precisely what this prototype is here to find out.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

# NOTE: pydantic_monty is intentionally NOT imported here. The Monty interpreter never runs
# in the workflow — it runs in the monty_*_batch activities. The workflow is a pure
# orchestrator (the shared MontyHostDriver) that threads opaque snapshot bytes between them.
with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import AgentConfig, TextReply, ToolApprovalPolicy
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from ._host_driver import MontyHostDriver
    from .models import RunScript


# The agent's own task queue. The (agent-agnostic) session manager launches this agent by
# its registered defn name on whichever queue the caller specifies — so this agent runs
# under its true name on its own queue; no masquerading as another agent.
TASK_QUEUE = "monty-dynamic-agent"


@workflow.defn(name="MontyDynamicAgent")
@agent.defn
class MontyDynamicAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Monty runs its tools inside a sandboxed simulation (no real-world side
            # effects), so it intentionally skips approvals by default. A caller can still
            # tighten this per session via AgentConfig.approval_policy.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # Shared execution half: runs the script via the async batch loop (composition).
        self._monty = MontyHostDriver(self._runner)

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def run_script(self, message: RunScript) -> TextReply:
        """Execute a Python script in the Monty sandbox and return its output + final value.

        The script MUST be async: the host functions are ``async``, so ``await`` them — and
        run independent calls concurrently with ``asyncio.gather`` — and wrap the body in
        ``asyncio.run(main())``. See :class:`RunScript` for the full sandbox contract, the
        async script structure, and the host functions available. A type/syntax/runtime error
        in the script is reported as the reply text (a bad script is normal input, not a
        failure). Execution (including concurrent host-call batches) is the shared
        :class:`~._host_driver.MontyHostDriver` held in ``self._monty``."""
        return TextReply(text=await self._monty.run_script(message.script))
