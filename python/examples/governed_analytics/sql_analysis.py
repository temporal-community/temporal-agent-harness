"""``analyze_sql``: the facts about a query that the tool policy judges it by.

A policy can't parse SQL, and a regex over table names is beaten by a CTE, an alias or a
subquery. So the facts come from DuckDB itself, on the same locked-down connection the tools
use:

  * the statements, from DuckDB's parser; nothing but a single ``SELECT`` is planned;
  * the tables, from the query's plan with the optimizer off, so every table the query reads
    is there, fully resolved (``query_table('risk.loan_applications')`` included);
  * the table functions, from both the parse tree and the plan (``information_schema`` views
    become catalog functions only in the plan).

A query that doesn't plan (an unknown table, a file the connection refuses to read) gets its
error as a fact rather than raising, so the policy can deny it with a reason the agent reads.
"""

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import duckdb
from temporalio import activity

from examples.governed_analytics.databases import connect
from examples.governed_analytics.models import SqlFacts

ANALYZE_SQL_ACTIVITY = "analyze_sql"


@activity.defn(name=ANALYZE_SQL_ACTIVITY)
async def analyze_sql(sql: str) -> SqlFacts:
    """The facts about ``sql`` the policy sees, read on a fresh analysis connection."""
    return await asyncio.to_thread(_analyze, sql)


def _analyze(sql: str) -> SqlFacts:
    with connect(optimize=False) as connection:
        return sql_facts(connection, sql)


def sql_facts(connection: duckdb.DuckDBPyConnection, sql: str) -> SqlFacts:
    """The facts about ``sql``, read on ``connection`` (see :func:`connect`, ``optimize=False``)."""
    try:
        statements = connection.extract_statements(sql)
    except duckdb.Error as e:
        return SqlFacts(statement="INVALID", error=str(e))
    if len(statements) != 1:
        return SqlFacts(statement="MULTIPLE" if statements else "INVALID")
    statement = statements[0].type.name
    if statement != "SELECT":
        return SqlFacts(statement=statement)

    [[serialized]] = connection.execute("SELECT json_serialize_sql(?)", [sql]).fetchall()
    functions = set(_parsed_table_functions(json.loads(serialized)))
    try:
        [[_, plan]] = connection.execute(f"EXPLAIN (FORMAT json) {sql}").fetchall()
    except duckdb.Error as e:
        return SqlFacts(statement=statement, table_functions=sorted(functions), error=str(e))
    tables: set[str] = set()
    for info in _operator_infos(json.loads(plan)):
        if "Table" in info:
            tables.add(_table_name(info["Table"]))
        elif "Function" in info:
            functions.add(str(info["Function"]).lower())
    return SqlFacts(statement=statement, tables=sorted(tables), table_functions=sorted(functions))


def _table_name(qualified: str) -> str:
    """``risk.main.loan_applications`` as ``risk.loan_applications``; other schemas are kept."""
    database, schema, table = qualified.split(".", 2)
    return f"{database}.{table}" if schema == "main" else qualified


def _parsed_table_functions(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        if node.get("type") == "TABLE_FUNCTION":
            name = (node.get("function") or {}).get("function_name")
            if name:
                yield str(name).lower()
        for value in node.values():
            yield from _parsed_table_functions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _parsed_table_functions(value)


def _operator_infos(node: Any) -> Iterator[dict[str, Any]]:
    """Each plan operator's ``extra_info``."""
    if isinstance(node, dict):
        info = node.get("extra_info")
        if isinstance(info, dict):
            yield info
        for value in node.get("children") or ():
            yield from _operator_infos(value)
    elif isinstance(node, list):
        for value in node:
            yield from _operator_infos(value)
