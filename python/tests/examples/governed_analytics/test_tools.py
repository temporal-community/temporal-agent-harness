# ABOUTME: Tests for the analytics tools' bodies on generated data: what they return, the
# connection's refusals that back up the policy (no file reads, no ATTACH, no SET, nothing but
# a single SELECT), and the story the generated data has to tell for the demo.

import duckdb
import pytest

from examples.governed_analytics import tools
from examples.governed_analytics.generate_data import QUARTERS, TROUBLED_COUNT


def test_list_and_describe() -> None:
    tables = {t.table for t in tools._list_tables("risk")}
    assert tables == {"credit_scores", "loan_applications"}
    described = tools._describe_table("risk.loan_applications")
    assert [c.name for c in described.columns][:3] == ["application_id", "applicant_id", "branch_id"]
    with pytest.raises(ValueError):
        tools._list_tables("main")
    with pytest.raises(ValueError):
        tools._describe_table("loan_applications")


def test_run_sql_caps_rows() -> None:
    result = tools._run_sql("SELECT * FROM transactions_daily")
    assert result.truncated and len(result.rows) == tools.MAX_ROWS
    assert result.columns == ["branch_id", "day", "transactions", "volume"]
    assert isinstance(result.rows[0][1], str)  # dates as ISO strings


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH ':memory:' AS scratch",
        "SET enable_external_access = true",
        "SELECT 1; SELECT 2",
        "COPY branches TO 'branches.csv'",
        "CREATE TABLE stolen AS SELECT * FROM branches",
    ],
)
def test_only_a_single_select_runs(sql: str) -> None:
    with pytest.raises(ValueError, match="single read-only SELECT"):
        tools._run_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('python/examples/governed_analytics/data/risk.duckdb')",
        "SELECT * FROM glob('/*')",
    ],
)
def test_the_connection_refuses_files(sql: str) -> None:
    with pytest.raises(duckdb.PermissionException):
        tools._run_sql(sql)


def test_a_column_name_cannot_inject_sql() -> None:
    with pytest.raises(duckdb.BinderException):
        tools._summary_stats(
            "SELECT * FROM branches", 'branch_id") FROM risk.loan_applications --'
        )


def _q3_change_sql() -> str:
    return f"""
        SELECT q.branch_id, q.deposit_volume / p.deposit_volume - 1 AS change
        FROM branch_deposits q JOIN branch_deposits p USING (branch_id)
        WHERE q.quarter = '{QUARTERS[-1]}' AND p.quarter = '{QUARTERS[-2]}'
    """


def test_statistics_tools() -> None:
    sql = _q3_change_sql()
    stats = tools._summary_stats(sql, "change")
    assert stats.count == 40 and stats.nulls == 0
    assert stats.min is not None and stats.min < -0.2
    distribution = tools._calc_distribution(sql, "change", 5)
    assert sum(b.count for b in distribution.bins) == 40
    correlation = tools._correlate(
        "SELECT deposit_volume, deposit_accounts FROM branch_deposits",
        ["deposit_volume", "deposit_accounts"],
    )
    assert correlation.matrix[0][0] == pytest.approx(1.0)
    assert correlation.matrix[0][1] is not None and correlation.matrix[0][1] > 0.5


def test_the_data_tells_the_story() -> None:
    """A handful of branches lost deposits last quarter, and their loans default more."""
    outliers = tools._detect_outliers(_q3_change_sql(), "change", "iqr")
    troubled = {row[0] for row in outliers.rows}
    assert len(troubled) == TROUBLED_COUNT

    ids = ", ".join(str(b) for b in sorted(troubled))
    comparison = tools._group_compare(
        f"SELECT branch_id IN ({ids}) AS troubled, defaulted::INTEGER AS defaulted "
        "FROM risk.loan_applications WHERE approved",
        "troubled",
        "defaulted",
    )
    [steady, struggling] = comparison.groups
    assert steady.group is False and struggling.group is True
    assert struggling.mean is not None and steady.mean is not None
    assert struggling.mean > 2 * steady.mean
    assert comparison.welch_t is not None and abs(comparison.welch_t) > 2
