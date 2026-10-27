# ABOUTME: Tests for analyze_sql's facts, the input the tool policy judges a query by: the
# tables a query reads however it hides them, its table functions, and every statement that
# isn't a single SELECT. These are the workaround attempts of the demo's third beat.

import pytest

from examples.governed_analytics.databases import connect
from examples.governed_analytics.models import SqlFacts
from examples.governed_analytics.sql_analysis import sql_facts


def facts(sql: str) -> SqlFacts:
    with connect(optimize=False) as connection:
        return sql_facts(connection, sql)


@pytest.mark.parametrize(
    ("sql", "tables"),
    [
        ("SELECT * FROM branch_deposits", ["analytics.branch_deposits"]),
        ("SELECT * FROM analytics.main.branches", ["analytics.branches"]),
        ('SELECT * FROM "risk"."loan_applications"', ["risk.loan_applications"]),
        ("FROM risk.loan_applications", ["risk.loan_applications"]),
        (
            "WITH x AS (SELECT * FROM risk.loan_applications l) "
            "SELECT * FROM x JOIN branch_deposits b USING (branch_id)",
            ["analytics.branch_deposits", "risk.loan_applications"],
        ),
        (
            "SELECT * FROM branches WHERE branch_id IN "
            "(SELECT branch_id FROM customer_pii.customers)",
            ["analytics.branches", "customer_pii.customers"],
        ),
        (
            "SELECT (SELECT max(score) FROM risk.credit_scores) AS top_score",
            ["risk.credit_scores"],
        ),
        # A CTE named like a restricted table is not that table.
        ("WITH loan_applications AS (SELECT 1 AS x) SELECT * FROM loan_applications", []),
        # The optimizer would drop this scan; the analysis keeps it.
        ("SELECT * FROM risk.loan_applications WHERE 1 = 0", ["risk.loan_applications"]),
    ],
)
def test_tables_however_the_query_names_them(sql: str, tables: list[str]) -> None:
    result = facts(sql)
    assert result.statement == "SELECT"
    assert result.tables == tables
    assert result.error is None


def test_query_table_reads_the_table_it_names() -> None:
    result = facts("SELECT * FROM query_table('risk.loan_applications')")
    assert result.tables == ["risk.loan_applications"]
    assert "query_table" in result.table_functions


@pytest.mark.parametrize(
    ("sql", "function"),
    [
        ("SELECT * FROM read_parquet('data/risk.parquet')", "read_parquet"),
        ("SELECT * FROM read_csv('/etc/passwd')", "read_csv"),
        (
            "SELECT * FROM branches WHERE branch_id IN (SELECT branch_id FROM read_csv('x.csv'))",
            "read_csv",
        ),
    ],
)
def test_file_reads_are_named_and_do_not_plan(sql: str, function: str) -> None:
    result = facts(sql)
    assert function in result.table_functions
    assert result.error is not None


def test_catalog_views_show_their_functions() -> None:
    assert "duckdb_tables" in facts("SELECT * FROM information_schema.tables").table_functions
    assert "duckdb_tables" in facts("SELECT * FROM duckdb_tables()").table_functions


@pytest.mark.parametrize(
    ("sql", "statement"),
    [
        ("ATTACH 'risk.duckdb' AS r2", "ATTACH"),
        ("COPY risk.loan_applications TO 'out.csv'", "COPY"),
        ("SET enable_external_access = true", "SET"),
        ("PRAGMA database_list", "PRAGMA"),
        ("INSTALL httpfs", "LOAD"),
        ("SELECT 1; SELECT * FROM risk.loan_applications", "MULTIPLE"),
    ],
)
def test_anything_but_a_single_select_is_named_and_not_planned(sql: str, statement: str) -> None:
    result = facts(sql)
    assert result.statement == statement
    assert result.tables == []


def test_unparseable_and_unknown() -> None:
    garbled = facts("SELEKT * FROM branches")
    assert garbled.statement == "INVALID"
    assert garbled.error
    unknown = facts("SELECT * FROM risk.no_such_table")
    assert unknown.statement == "SELECT"
    assert unknown.error and "no_such_table" in unknown.error
