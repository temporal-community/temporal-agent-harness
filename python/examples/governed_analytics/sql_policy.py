"""What the tool policy sees of a SQL tool's call: the query's facts, and a check per table.

``sql_policy_calls`` is the ``dogwood_evaluator`` policy call builder for every tool that takes
a query. It runs ``analyze_sql`` as an activity, so the facts are in workflow history beside
the decision they led to, and sends:

  * the call itself, ``<prefix>/<tool>``, with the query's :class:`SqlFacts` as ``arg0``, so
    a policy can refuse anything but a single SELECT, file-reading table functions, and so on;
  * one ``<prefix>/read_table`` check per table the query reads, with ``{table, tool}``.

Dogwood's temporal patterns match a field by equality, so a grant naming one table can only
match a check about one table: hence a check per table. The verifier decides a request's calls
together, so the query runs only when every table it reads may be read.
"""

from datetime import timedelta

from temporalio import workflow

from temporal_agent_harness.harness.agent_protocol import AutoApprovalContext
from temporal_agent_harness.harness.dogwood_approvals import (
    PolicyCall,
    PolicyCallBuilder,
    PolicyCalls,
)

from examples.governed_analytics.models import SqlFacts, TableRead

with workflow.unsafe.imports_passed_through():
    from examples.governed_analytics.sql_analysis import analyze_sql
    from examples.governed_analytics.tools import SQL_TOOLS

READ_TABLE = "read_table"


async def sql_policy_calls(ctx: AutoApprovalContext) -> PolicyCalls:
    facts = await workflow.execute_activity(
        analyze_sql,
        ctx.tool_input["sql"],
        start_to_close_timeout=timedelta(seconds=10),
        summary=f"analyze the query of {ctx.tool_name}",
    )
    return PolicyCalls(
        arg0=facts.model_dump(mode="json", exclude_none=True),
        checks=[
            PolicyCall(
                tool=READ_TABLE,
                arg0=TableRead(table=table, tool=ctx.tool_name).model_dump(mode="json"),
            )
            for table in facts.tables
        ],
        # A query that doesn't plan is refused; DuckDB's error tells the agent what to fix.
        explanation=f"DuckDB: {facts.error}" if facts.error else None,
    )


# For dogwood_evaluator(policy_calls=...).
SQL_POLICY_CALLS: dict[str, PolicyCallBuilder] = {
    tool.__name__: sql_policy_calls for tool in SQL_TOOLS
}

# For dogwood_schema(call_inputs=...): the shapes the builder sends.
SQL_CALL_INPUTS = {
    **{tool.__name__: SqlFacts for tool in SQL_TOOLS},
    READ_TABLE: TableRead,
}
