"""Demo native subagent for the Nexus-hello example: a harness agent exposed directly over
Nexus through its own AgentServiceHandler -- the same contract that fronts the Slack
connector. No gateway, no registration. The orchestrator reaches it directly through
agent.nexus_native_subagent() / NexusTransport.

Parallels nexus_tool_service.py's role (a Nexus-native resource, no gateway) for a
different resource kind: a whole subagent, not a tool.

The workflow and its Nexus front door run in ONE worker, ONE task queue here -- proving
that's possible (nexus_agent_adapter/worker.py's own docstring calls this out as a valid
alternative to a standalone Nexus worker; the Slack connector uses the standalone form
instead, to scale Nexus traffic independently of workflow execution).

No model here -- a canned reply, so this demo needs no API key.

Run with (from the repo root):
    uv run --group examples python -m examples.nexus_hello.native_subagent worker
"""

from __future__ import annotations

import asyncio
import sys

from temporalio import workflow
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.nexus_agent_adapter.handler import AgentServiceHandler, Config

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from temporalio.contrib.workflow_streams import WorkflowStream

TASK_QUEUE = "nexus-hello-subagent"


@workflow.defn(name="NativeResearchSubagent")
@agent.defn
class NativeResearchSubagentWorkflow:
    """A minimal subagent, reached directly over Nexus (no gateway)."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Answer a question. Canned reply -- this demo has no real model."""
        return TextReply(
            text=f"[native subagent] you asked {message.text!r} -- here is a canned research answer."
        )


async def _run_worker() -> None:
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config)
    config = Config(
        agent_task_queue=TASK_QUEUE,
        workflow_name="NativeResearchSubagent",
        workflow_id_prefix="",  # NexusTransport's session_id IS the workflow id already
        is_message_queuing_enabled=True,
    )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[NativeResearchSubagentWorkflow],
        nexus_service_handlers=[AgentServiceHandler(client, config)],
    )
    print(
        f"Native research subagent worker ready (workflow + Nexus front door): "
        f"taskQueue={TASK_QUEUE!r}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] != "worker":
        sys.exit("usage: python -m examples.nexus_hello.native_subagent worker")
    asyncio.run(_run_worker())
