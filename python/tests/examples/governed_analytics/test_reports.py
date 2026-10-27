# ABOUTME: Tests for publishing reports and what the policy needs to refuse one: every SQL
# tool's result says which tables its query read and whether any was restricted, and the
# policy sees those results and the publish_report call.

from examples.governed_analytics import tools
from examples.governed_analytics.reports import publish_report
from examples.governed_analytics.workflow import ACTION_PREFIX
from temporal_agent_harness.harness.dogwood_approvals import dogwood_schema


def _action(schema: str, name: str) -> str:
    start = schema.index(f'action "{name}"')
    return schema[start : schema.index("};", start)]


def test_a_result_says_what_its_query_read() -> None:
    open_result = tools._reading(tools._run_sql, "SELECT * FROM branch_deposits LIMIT 1")
    assert open_result.tables_read == ["analytics.branch_deposits"]
    assert open_result.restricted_data is False

    restricted = tools._reading(
        tools._summary_stats,
        "WITH l AS (SELECT * FROM risk.loan_applications) "
        "SELECT b.branch_id, l.amount FROM branches b JOIN l USING (branch_id)",
        "amount",
    )
    assert restricted.tables_read == ["analytics.branches", "risk.loan_applications"]
    assert restricted.restricted_data is True


def test_the_policy_sees_restricted_reads_and_publishing() -> None:
    schema = dogwood_schema([*tools.SQL_TOOLS, publish_report], action_prefix=ACTION_PREFIX)
    for tool in tools.SQL_TOOLS:
        result = _action(schema, f"{ACTION_PREFIX}/{tool.__name__}/result")
        assert "restricted_data?: Bool," in result, tool.__name__
    publish = _action(schema, f"{ACTION_PREFIX}/publish_report")
    assert "title?: String," in publish
