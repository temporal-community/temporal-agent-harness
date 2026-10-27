"""Write the Dogwood action schema for this example to ``policies/schema.cedarschema``.

Run `just schema` after changing the agent's tools. The verifier validates
``policies/bank.dw`` against the schema at startup, so a policy that names a tool or field the
agent doesn't send fails there rather than silently never matching.
"""

from __future__ import annotations

from pathlib import Path

from temporal_agent_harness.harness.dogwood_approvals import dogwood_schema

from examples.governed_analytics.access import ask_for_access, request_access
from examples.governed_analytics.charts import CHART_TOOLS
from examples.governed_analytics.reports import publish_report
from examples.governed_analytics.sql_policy import SQL_CALL_INPUTS
from examples.governed_analytics.tools import ALL_TOOLS
from examples.governed_analytics.workflow import ACTION_PREFIX, GovernedAnalyticsAgentWorkflow

# Relative to the repo root, where `bazel run` starts the program.
SCHEMA_FILE = Path("python/examples/governed_analytics/policies/schema.cedarschema")


def render() -> str:
    """The data tools (the SQL tools as their queries' facts), the read_table checks, the
    access tools, and the agent's messages. The script tool is pre-approved and its result is
    read by no policy, so it is left out."""
    return dogwood_schema(
        [*ALL_TOOLS, request_access, ask_for_access, *CHART_TOOLS, publish_report],
        action_prefix=ACTION_PREFIX,
        call_inputs=SQL_CALL_INPUTS,
        agent_class=GovernedAnalyticsAgentWorkflow,
    )


if __name__ == "__main__":
    SCHEMA_FILE.write_text(render())
    print(f"wrote {SCHEMA_FILE}")
