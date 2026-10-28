"""The analytics agent's tools: its only capabilities.

Each is an ``@agent.activity_tool_defn``, so a Code Mode script calls it as a host function
and each call reaches the approval gate, and the tool policy, on its own. Every statistics tool
takes a SQL query as its data source rather than a table name, so each is "``run_sql`` plus
math" and is policed the same way (see ``sql_policy.py``).

What each tool does is in ``queries.py``; this file gives the agent its tools, and the
docstrings the model reads.

No ``from __future__ import annotations`` here, as in the other examples' Temporal-facing
modules (stringized annotations trip the pydantic converter).
"""

import asyncio
from datetime import timedelta
from typing import Literal

from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent

from examples.governed_analytics import queries
from examples.governed_analytics.models import (
    Correlation,
    Distribution,
    GroupComparison,
    Outliers,
    QueryResult,
    SummaryStats,
    TableDescription,
    TableInfo,
)

_CONFIG = ActivityConfig(start_to_close_timeout=timedelta(seconds=30))


@agent.activity_tool_defn(name="list_tables", activity_config=_CONFIG)
async def list_tables(database: str) -> list[TableInfo]:
    """List the tables in one database (`analytics`, `risk` or `customer_pii`), with their row
    counts."""
    return await asyncio.to_thread(queries.list_tables, database)


@agent.activity_tool_defn(name="describe_table", activity_config=_CONFIG)
async def describe_table(table: str) -> TableDescription:
    """Describe a table's columns. `table` is `<database>.<table>`, e.g.
    `analytics.branch_deposits`."""
    return await asyncio.to_thread(queries.describe_table, table)


@agent.activity_tool_defn(name="run_sql", activity_config=_CONFIG)
async def run_sql(sql: str) -> QueryResult:
    """Run one read-only SELECT (DuckDB SQL) and return up to 200 rows. Tables are named
    `<database>.<table>`; unqualified names are in `analytics`."""
    return await asyncio.to_thread(queries.run_sql, sql)


@agent.activity_tool_defn(name="summary_stats", activity_config=_CONFIG)
async def summary_stats(sql: str, column: str) -> SummaryStats:
    """Count, nulls, mean, standard deviation, min, quartiles and max of one numeric column of
    a SELECT's result."""
    return await asyncio.to_thread(queries.summary_stats, sql, column)


@agent.activity_tool_defn(name="calc_distribution", activity_config=_CONFIG)
async def calc_distribution(sql: str, column: str, bins: int = 10) -> Distribution:
    """Histogram of one numeric column of a SELECT's result, in `bins` equal-width bins."""
    return await asyncio.to_thread(queries.calc_distribution, sql, column, bins)


@agent.activity_tool_defn(name="correlate", activity_config=_CONFIG)
async def correlate(sql: str, columns: list[str]) -> Correlation:
    """Pearson correlation matrix of two or more numeric columns of a SELECT's result."""
    return await asyncio.to_thread(queries.correlate, sql, columns)


@agent.activity_tool_defn(name="group_compare", activity_config=_CONFIG)
async def group_compare(sql: str, group_column: str, value_column: str) -> GroupComparison:
    """Compare a numeric column across the groups of another column of a SELECT's result:
    count, mean and standard deviation per group, and Welch's t statistic when there are
    exactly two groups."""
    return await asyncio.to_thread(queries.group_compare, sql, group_column, value_column)


@agent.activity_tool_defn(name="detect_outliers", activity_config=_CONFIG)
async def detect_outliers(
    sql: str,
    column: str,
    method: Literal["iqr", "zscore"] = "iqr",
    threshold: float | None = None,
) -> Outliers:
    """The rows of a SELECT's result whose `column` is an outlier: beyond `threshold` (default
    1.5) interquartile ranges outside the quartiles (`iqr`), or more than `threshold` (default
    3) standard deviations from the mean (`zscore`)."""
    return await asyncio.to_thread(
        queries.detect_outliers, sql, column, method, threshold
    )


SQL_TOOLS = [run_sql, summary_stats, calc_distribution, correlate, group_compare, detect_outliers]
ALL_TOOLS = [list_tables, describe_table, *SQL_TOOLS]
