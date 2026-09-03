"""Run the native subagent for the Nexus hello example.

One worker hosts the agent workflow and its Nexus service. The subagent returns a fixed
reply and does not call a model.

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

with workflow.unsafe.imports_passed_through():
    from nexus_a2a import a2a_nexus_data_converter
    from temporalio.contrib.workflow_streams import WorkflowStream

    from temporal_agent_harness.a2a.adapter import (
        A2AHandlerConfig,
        A2AServiceHandler,
        make_agent_card,
    )
    from temporal_agent_harness.a2a.control_handler import (
        HarnessControlConfig,
        HarnessControlServiceHandler,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

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
    client = await Client.connect(
        **connect_config, data_converter=a2a_nexus_data_converter
    )
    control_config = HarnessControlConfig()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[NativeResearchSubagentWorkflow],
        nexus_service_handlers=[
            A2AServiceHandler(
                client,
                A2AHandlerConfig(
                    agent_task_queue=TASK_QUEUE,
                    workflow_name="NativeResearchSubagent",
                    workflow_id_prefix="",
                    is_message_queuing_enabled=True,
                    agent_card=make_agent_card(
                        name="Research",
                        description="A concise research agent.",
                        endpoint="nexus-hello-subagent-endpoint",
                    ),
                ),
            ),
            HarnessControlServiceHandler(client, control_config),
        ],
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
