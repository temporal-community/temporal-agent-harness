"""``BranchReport``: the bank's branch reporting job, as a plain workflow with no agent.

It runs a few queries over the bank's data, then publishes a report anyone with the link can
read:

  1. how branch deposits changed last quarter;
  2. which branches are outliers;
  3. who those branches are;
  4. publish the report.

Every step goes through a :class:`PolicyGate`, which asks the Dogwood verifier, with the
example's policy (``policies/bank.dw``), before it runs and records its result after. The
reporting job is entitled to read risk data, so no query is refused. What the policy refuses
is a public report from a run that has read it: uncomment the join in step 3's query and the
run fails at step 4, its report unpublished, though nothing in this file changed but the query.

No ``from __future__ import annotations`` here (stringized annotations trip the pydantic
converter).
"""

from datetime import timedelta
from typing import Any

from temporalio import workflow

from examples.governed_analytics.static.policy_gate import (
    PolicyCheck,
    PolicyGate,
    PolicyInput,
)

with workflow.unsafe.imports_passed_through():
    # Passed through, as the activities import them, so the results they return are these
    # classes, not the sandbox's own copies of them.
    from examples.governed_analytics.models import (
        PublishedReport,
        QueryResult,
        ReportSection,
        SummaryStats,
        TableRead,
    )
    from examples.governed_analytics.sql_analysis import analyze_sql
    from examples.governed_analytics.static.activities import (
        SQL_ACTIVITIES,
        detect_outliers,
        publish_report,
        run_sql,
        summary_stats,
    )

TASK_QUEUE = "governed-analytics-static"
# As the agent's: calls reach the policy as "bank-analyst/<activity>", through the same endpoint.
ACTION_PREFIX = "bank-analyst"
POLICY_ENDPOINT = "tool-verifier"
READ_TABLE = "read_table"

async def sql_policy_input(activity: str, arguments: dict[str, Any]) -> PolicyInput:
    """What the policy sees of a SQL activity's call, as the agent's ``sql_policy.py`` builds
    it: the query's facts as ``arg0``, and one ``read_table`` check per table it reads."""
    facts = await workflow.execute_activity(
        analyze_sql,
        arguments["sql"],
        start_to_close_timeout=timedelta(seconds=10),
        summary=f"analyze the query of {activity}",
    )
    return PolicyInput(
        arg0=facts.model_dump(mode="json", exclude_none=True),
        checks=[
            PolicyCheck(
                tool=READ_TABLE,
                arg0=TableRead(table=table, tool=activity).model_dump(mode="json"),
            )
            for table in facts.tables
        ],
    )


# workflow.execute_activity, once the tool policy allows the call.
execute_activity = PolicyGate(
    endpoint=POLICY_ENDPOINT,
    action_prefix=ACTION_PREFIX,
    builders={activity.__name__: sql_policy_input for activity in SQL_ACTIVITIES},
).execute


@workflow.defn(name="BranchReport")
class BranchReport:

    @workflow.run
    async def run(self) -> PublishedReport:
        # Each branch's deposit change from the second-to-last quarter to the last.
        deposit_change_sql = """
            WITH quarters AS (
                SELECT DISTINCT quarter FROM branch_deposits ORDER BY quarter DESC LIMIT 2
            )
            SELECT q.branch_id, q.deposit_volume / p.deposit_volume - 1 AS change
            FROM branch_deposits q JOIN branch_deposits p USING (branch_id)
            WHERE q.quarter = (SELECT max(quarter) FROM quarters)
              AND p.quarter = (SELECT min(quarter) FROM quarters)
        """
        # 1. How branch deposits changed last quarter.
        change = await execute_activity(
            summary_stats, deposit_change_sql, "change",
            summary="deposit change per branch, last quarter",
        )
        # 2. Which branches are outliers.
        outliers = await execute_activity(
            detect_outliers, deposit_change_sql, "change", "iqr", None,
            summary="branches whose deposits changed unusually",
        )
        ids = ", ".join(str(row[0]) for row in outliers.rows) or "NULL"
        # 3. Who those branches are. Uncomment the three `--` lines to add each branch's loan
        # default rate, from risk.loan_applications.
        branches = await execute_activity(
            run_sql,
            f"""
            SELECT b.branch_id, b.branch_name, b.region
                -- , avg(l.defaulted::INTEGER) AS loan_default_rate
            FROM branches b
            -- JOIN risk.loan_applications l USING (branch_id)
            WHERE b.branch_id IN ({ids})
            -- GROUP BY ALL
            ORDER BY b.branch_id
            """,
            summary="the outlier branches",
        )
        # 4. Publish the report.
        return await execute_activity(
            publish_report,
            "Branch deposits, last quarter",
            f"{len(branches.rows)} branches' deposits moved far outside the rest of the bank's.",
            [
                ReportSection(heading="Deposit change", body=_describe_change(change)),
                ReportSection(heading="The outlier branches", body=_markdown_table(branches)),
            ],
            summary="publish the report",
        )


def _describe_change(change: SummaryStats) -> str:
    return (
        f"Median change {_percent(change.median)}, from {_percent(change.min)} "
        f"to {_percent(change.max)} across {change.count} branches."
    )


def _markdown_table(result: QueryResult) -> str:
    rows = [result.columns, ["---"] * len(result.columns), *result.rows]
    return "\n".join("| " + " | ".join(str(value) for value in row) + " |" for row in rows)


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1%}"
