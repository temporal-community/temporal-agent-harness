# ABOUTME: Smoke test for the governed analytics agent's workflow under the default
# (sandboxed) runner, as the worker runs it: the agent starts, its imports (DuckDB-backed tools,
# the Gemini plugin, the Dogwood evaluator) pass through the sandbox, and it answers queries.
# No model call and no policy service: nothing is sent to the agent.

import uuid

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness.agent_protocol import AGENT_INTERFACE_QUERY, AgentConfig
from temporal_agent_harness.plugin import AgentHarnessPlugin

from examples.governed_analytics.sql_analysis import analyze_sql
from examples.governed_analytics.tools import ALL_TOOLS
from examples.governed_analytics.workflow import GovernedAnalyticsAgentWorkflow


async def test_the_agent_starts_in_the_sandbox() -> None:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        task_queue = f"governed-analytics-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[GovernedAnalyticsAgentWorkflow],
            activities=[analyze_sql],
            plugins=[AgentHarnessPlugin(tools=ALL_TOOLS)],
        ):
            handle = await env.client.start_workflow(
                GovernedAnalyticsAgentWorkflow.run,
                AgentConfig(),
                id=f"governed-analytics-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            interface = await handle.query(AGENT_INTERFACE_QUERY)
            await handle.terminate()
    assert "ask" in str(interface)
