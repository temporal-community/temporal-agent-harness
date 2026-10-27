# ABOUTME: Tests for access to restricted tables, which a person grants for the session and can
# revoke: the request_access tool (asking through the ask_for_access callback) and the
# grant_access / revoke_access messages. The person picks tables from a list, the access board
# shows where each table stands, and the policy sees the table of every grant and revocation.

import typing

from pydantic import TypeAdapter

from examples.governed_analytics.access import (
    AccessAnswer,
    AccessRequest,
    RestrictedTable,
    TableAccessBoard,
    record_access,
    request_access,
)
from examples.governed_analytics.workflow import ACTION_PREFIX, GovernedAnalyticsAgentWorkflow
from temporal_agent_harness.harness.dogwood_approvals import dogwood_schema
from temporal_agent_harness.harness.state import StateRef


def _action(schema: str, name: str) -> str:
    start = schema.index(f'action "{name}"')
    return schema[start : schema.index("};", start)]


def test_the_person_picks_from_lists() -> None:
    answer = TypeAdapter(AccessAnswer).json_schema()
    assert answer["properties"]["access"]["enum"] == ["Grant for this session", "Decline"]
    access = TypeAdapter(AccessRequest).json_schema()
    assert access["properties"]["table"]["enum"] == list(typing.get_args(RestrictedTable))


def test_the_board_shows_where_each_table_stands() -> None:
    board = StateRef("table_access", TableAccessBoard())
    assert {row.table: row.status for row in board.current.tables} == {
        "risk.loan_applications": "needs access",
        "risk.credit_scores": "needs access",
        "customer_pii.customers": "never",
    }
    record_access(
        board, "risk.loan_applications", "granted", via="the agent's request", reason="defaults"
    )
    record_access(board, "risk.credit_scores", "revoked", via="your message")
    rows = {row.table: row for row in board.current.tables}
    loans = rows["risk.loan_applications"]
    assert (loans.status, loans.via, loans.reason) == ("granted", "the agent's request", "defaults")
    assert rows["risk.credit_scores"].status == "revoked"


def test_the_model_sees_only_the_table_and_reason() -> None:
    assert list(request_access.__annotations__) == ["table", "reason", "return"]


def test_the_policy_sees_the_table_of_every_grant_and_revocation() -> None:
    schema = dogwood_schema(
        [request_access], action_prefix=ACTION_PREFIX, agent_class=GovernedAnalyticsAgentWorkflow
    )
    requested = _action(schema, "bank-analyst/request_access/result")
    assert "arg1?: {" in requested and "table?: String," in requested
    for message in ("update/grant_access", "update/revoke_access"):
        assert "table?: String," in _action(schema, message)
