"""Write the Cedar schema for this example's tool calls to ``policies/schema.cedarschema``.

Run from the repo root after changing the agent's tools (or `just schema` here):

    uv run python -m examples.auto_mode_cedar.schema

The verifier validates ``policies/travel.cedar`` against the schema at startup, so a policy that
names a tool or parameter the agent doesn't have fails there rather than silently never matching.
"""

from __future__ import annotations

from pathlib import Path

from temporal_agent_harness.harness.cedar_approvals.schema import cedar_schema
from temporal_agent_harness.harness.jev_approvals import DEFAULT_SCRIPT_FEATURES

from ..monty import activities
from .workflow import ACTION_PREFIX, SCRIPT_TOOLS, build_code_tool

SCHEMA_FILE = Path(__file__).parent / "policies" / "schema.cedarschema"
HOST_FUNCTIONS = frozenset(tool.__name__ for tool in SCRIPT_TOOLS)


def render() -> str:
    """The schema: the script tool, and each travel tool its host calls reach the policy as.
    The board tools are pre-approved, so they never do."""
    return cedar_schema(
        [build_code_tool(), *activities.ALL_TOOLS],
        action_prefix=ACTION_PREFIX,
        script_features=DEFAULT_SCRIPT_FEATURES,
    )


if __name__ == "__main__":
    SCHEMA_FILE.write_text(render())
    print(f"wrote {SCHEMA_FILE}")
