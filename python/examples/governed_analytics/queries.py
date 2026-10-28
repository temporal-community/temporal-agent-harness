"""What the data tools do: plain, blocking functions over the bank's databases.

The agent's tools (``tools.py``) and the static flow's activities (``static/activities.py``)
both run these, each wrapped its own way; nothing here knows about either.

Every call opens a fresh locked-down connection (``databases.connect``) and refuses anything but
a single ``SELECT`` before running it, whatever the policy said: the policy decides what may
be read, and these make sure nothing else can be done. Every statistics function takes a SQL
query as its data source rather than a table name, so each is "``run_sql`` plus math", and
each result says which tables its query read.
"""

import datetime
import decimal
import functools
import math
import uuid
from collections.abc import Callable, Sequence
from typing import Concatenate, Literal, ParamSpec, TypeVar

import duckdb
from pydantic import JsonValue

from examples.governed_analytics.databases import DATABASES, connect
from examples.governed_analytics.models import (
    ColumnInfo,
    Correlation,
    DataResult,
    Distribution,
    GroupComparison,
    GroupStats,
    HistogramBin,
    Outliers,
    QueryResult,
    SummaryStats,
    TableDescription,
    TableInfo,
)
from examples.governed_analytics.sql_analysis import sql_facts

MAX_ROWS = 200

P = ParamSpec("P")
R = TypeVar("R", bound=DataResult)


def _reading(query: Callable[Concatenate[str, P], R]) -> Callable[Concatenate[str, P], R]:
    """``query``, saying on its result which tables its ``sql`` read."""

    @functools.wraps(query)
    def reading(sql: str, *args: P.args, **kwargs: P.kwargs) -> R:
        result = query(sql, *args, **kwargs)
        with connect(optimize=False) as connection:
            tables = sql_facts(connection, sql).tables
        restricted = any(not table.startswith("analytics.") for table in tables)
        return result.model_copy(update={"tables_read": tables, "restricted_data": restricted})

    return reading


def list_tables(database: str) -> list[TableInfo]:
    if database not in DATABASES:
        raise ValueError(f"unknown database {database!r}; the databases are {list(DATABASES)}")
    with connect() as connection:
        rows = connection.execute(
            "SELECT table_name, estimated_size FROM duckdb_tables() "
            "WHERE database_name = ? AND schema_name = 'main' ORDER BY table_name",
            [database],
        ).fetchall()
    return [TableInfo(database=database, table=name, rows=size) for name, size in rows]


def describe_table(table: str) -> TableDescription:
    database, _, name = table.partition(".")
    with connect() as connection:
        rows = connection.execute(
            "SELECT column_name, data_type, is_nullable FROM duckdb_columns() "
            "WHERE database_name = ? AND schema_name = 'main' AND table_name = ? "
            "ORDER BY column_index",
            [database, name],
        ).fetchall()
    if not rows:
        raise ValueError(f"no table {table!r}; name it as <database>.<table>")
    return TableDescription(
        table=table,
        columns=[ColumnInfo(name=c, type=t, nullable=n) for c, t, n in rows],
    )


@_reading
def run_sql(sql: str) -> QueryResult:
    with connect() as connection:
        relation = _select(connection, sql)
        rows = relation.limit(MAX_ROWS + 1).fetchall()
        return QueryResult(
            columns=list(relation.columns),
            rows=[[_json(v) for v in row] for row in rows[:MAX_ROWS]],
            truncated=len(rows) > MAX_ROWS,
        )


@_reading
def summary_stats(sql: str, column: str) -> SummaryStats:
    c = _ident(column)
    with connect() as connection:
        [row] = _select(connection, sql).query(
            "source",
            f"SELECT count({c}), count(*) - count({c}), avg({c}), stddev_samp({c}), min({c}), "
            f"quantile_cont({c}, 0.25), median({c}), quantile_cont({c}, 0.75), max({c}) "
            "FROM source",
        ).fetchall()
    count, nulls, *values = row
    mean, std, low, p25, median, p75, high = (_float(v) for v in values)
    return SummaryStats(
        column=column, count=count, nulls=nulls, mean=mean, std=std, min=low, p25=p25,
        median=median, p75=p75, max=high,
    )


@_reading
def calc_distribution(sql: str, column: str, bins: int = 10) -> Distribution:
    if not 1 <= bins <= 100:
        raise ValueError("bins must be between 1 and 100")
    values = [v for v in _column_values(sql, column) if v is not None]
    if not values:
        return Distribution(column=column, count=0, bins=[])
    low, high = min(values), max(values)
    width = (high - low) / bins or 1.0
    counts = [0] * bins
    for value in values:
        counts[min(int((value - low) / width), bins - 1)] += 1
    return Distribution(
        column=column,
        count=len(values),
        bins=[
            HistogramBin(lower=low + i * width, upper=low + (i + 1) * width, count=n)
            for i, n in enumerate(counts)
        ],
    )


@_reading
def correlate(sql: str, columns: list[str]) -> Correlation:
    if len(columns) < 2:
        raise ValueError("name at least two columns")
    pairs = [(a, b) for a in columns for b in columns]
    select = ", ".join(f"corr({_ident(a)}, {_ident(b)})" for a, b in pairs)
    with connect() as connection:
        [row] = _select(connection, sql).query("source", f"SELECT {select} FROM source").fetchall()
    values = [_float(v) for v in row]
    n = len(columns)
    return Correlation(columns=columns, matrix=[values[i * n : (i + 1) * n] for i in range(n)])


@_reading
def group_compare(sql: str, group_column: str, value_column: str) -> GroupComparison:
    g, v = _ident(group_column), _ident(value_column)
    with connect() as connection:
        rows = _select(connection, sql).query(
            "source",
            f"SELECT {g}, count({v}), avg({v}), stddev_samp({v}) FROM source "
            f"GROUP BY {g} ORDER BY {g} LIMIT {MAX_ROWS}",
        ).fetchall()
    groups = [
        GroupStats(group=_json(group), count=n, mean=_float(mean), std=_float(std))
        for group, n, mean, std in rows
    ]
    comparison = GroupComparison(
        group_column=group_column, value_column=value_column, groups=groups
    )
    if len(groups) == 2:
        comparison.welch_t, comparison.welch_df = _welch(*groups)
    return comparison


@_reading
def detect_outliers(
    sql: str,
    column: str,
    method: Literal["iqr", "zscore"] = "iqr",
    threshold: float | None = None,
) -> Outliers:
    c = _ident(column)
    with connect() as connection:
        relation = _select(connection, sql)
        if method == "iqr":
            k = 1.5 if threshold is None else threshold
            [[p25, p75]] = relation.query(
                "source",
                f"SELECT quantile_cont({c}, 0.25), quantile_cont({c}, 0.75) FROM source",
            ).fetchall()
            p25, p75 = _float(p25), _float(p75)
            bounds = (None, None) if p25 is None or p75 is None else (
                p25 - k * (p75 - p25), p75 + k * (p75 - p25)
            )
        else:
            k = 3.0 if threshold is None else threshold
            [[mean, std]] = relation.query(
                "source", f"SELECT avg({c}), stddev_samp({c}) FROM source"
            ).fetchall()
            mean, std = _float(mean), _float(std)
            bounds = (None, None) if mean is None or std is None else (
                mean - k * std, mean + k * std
            )
        low, high = bounds
        if low is None or high is None:
            return Outliers(
                column=column, method=method, lower_bound=None, upper_bound=None,
                columns=list(relation.columns), rows=[], truncated=False,
            )
        result = relation.query(
            "source", f"SELECT * FROM source WHERE {c} < {low!r} OR {c} > {high!r}"
        )
        rows = result.limit(MAX_ROWS + 1).fetchall()
        return Outliers(
            column=column,
            method=method,
            lower_bound=low,
            upper_bound=high,
            columns=list(result.columns),
            rows=[[_json(v) for v in row] for row in rows[:MAX_ROWS]],
            truncated=len(rows) > MAX_ROWS,
        )


def _select(connection: duckdb.DuckDBPyConnection, sql: str) -> duckdb.DuckDBPyRelation:
    """``sql`` as a relation, refusing anything but a single SELECT."""
    statements = connection.extract_statements(sql)
    if len(statements) != 1 or statements[0].type is not duckdb.StatementType.SELECT:
        raise ValueError("only a single read-only SELECT statement can run")
    return connection.sql(sql)


def _column_values(sql: str, column: str) -> list[float | None]:
    with connect() as connection:
        rows = _select(connection, sql).query(
            "source", f"SELECT {_ident(column)} FROM source"
        ).fetchall()
    return [_float(value) for (value,) in rows]


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _float(value: object) -> float | None:
    if value is None:
        return None
    result = float(value)  # type: ignore[arg-type]
    return None if math.isnan(result) else result


def _json(value: object) -> JsonValue:
    """A DuckDB value as JSON: dates as ISO strings, decimals as floats."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    return str(value)


def _welch(a: GroupStats, b: GroupStats) -> tuple[float | None, float | None]:
    if None in (a.mean, a.std, b.mean, b.std) or a.count < 2 or b.count < 2:
        return None, None
    va, vb = a.std**2 / a.count, b.std**2 / b.count  # type: ignore[operator]
    if va + vb == 0:
        return None, None
    t = (a.mean - b.mean) / math.sqrt(va + vb)  # type: ignore[operator]
    df = (va + vb) ** 2 / (va**2 / (a.count - 1) + vb**2 / (b.count - 1))
    return t, df
