"""The data tools as plain Temporal activities: the same queries the agent's tools run
(``queries.py``), under the same names, so the tool policy names them the same way. And
``publish_report``, which publishes nothing: whether the policy lets it run is the point.

No ``from __future__ import annotations`` here (stringized annotations trip the pydantic
converter).
"""

import asyncio
import uuid
from typing import Literal

from temporalio import activity

from examples.governed_analytics import queries
from examples.governed_analytics.models import (
    Correlation,
    Distribution,
    GroupComparison,
    Outliers,
    PublishedReport,
    QueryResult,
    ReportSection,
    SummaryStats,
    TableDescription,
    TableInfo,
)


@activity.defn
async def list_tables(database: str) -> list[TableInfo]:
    return await asyncio.to_thread(queries.list_tables, database)


@activity.defn
async def describe_table(table: str) -> TableDescription:
    return await asyncio.to_thread(queries.describe_table, table)


@activity.defn
async def run_sql(sql: str) -> QueryResult:
    return await asyncio.to_thread(queries.run_sql, sql)


@activity.defn
async def summary_stats(sql: str, column: str) -> SummaryStats:
    return await asyncio.to_thread(queries.summary_stats, sql, column)


@activity.defn
async def calc_distribution(sql: str, column: str, bins: int = 10) -> Distribution:
    return await asyncio.to_thread(queries.calc_distribution, sql, column, bins)


@activity.defn
async def correlate(sql: str, columns: list[str]) -> Correlation:
    return await asyncio.to_thread(queries.correlate, sql, columns)


@activity.defn
async def group_compare(sql: str, group_column: str, value_column: str) -> GroupComparison:
    return await asyncio.to_thread(queries.group_compare, sql, group_column, value_column)


@activity.defn
async def detect_outliers(
    sql: str,
    column: str,
    method: Literal["iqr", "zscore"] = "iqr",
    threshold: float | None = None,
) -> Outliers:
    return await asyncio.to_thread(queries.detect_outliers, sql, column, method, threshold)


@activity.defn
async def publish_report(
    title: str, summary: str, sections: list[ReportSection]
) -> PublishedReport:
    """Pretend to publish a report anyone with the link can read, as the agent's studio does."""
    report_id = uuid.uuid4().hex[:12]
    return PublishedReport(report_id=report_id, url=f"https://reports.example/{report_id}")


SQL_ACTIVITIES = [
    run_sql, summary_stats, calc_distribution, correlate, group_compare, detect_outliers
]
ALL_ACTIVITIES = [list_tables, describe_table, *SQL_ACTIVITIES, publish_report]
