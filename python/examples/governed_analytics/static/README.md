# A report that shouldn't be published, without an agent

`BranchReport` is the bank's branch reporting job: a plain Temporal workflow, no agent, no
model, no agent harness. It runs three queries over the bank's data and publishes a report
anyone with the link can read. Before every step, it asks the same Dogwood verifier as the
agent, with the same policy (`../policies/bank.dw`), and after every step it tells the verifier
what the step returned.

| Step | Reads |
|---|---|
| How branch deposits changed last quarter | `analytics.branch_deposits` |
| Which branches are outliers | `analytics.branch_deposits` |
| Who those branches are | `analytics.branches` |
| Publish the report | nothing |

## The demo

1. Run it. Every step is allowed, the report is published, and the run finishes in seconds.
2. Stop the worker. In `workflow.py`, uncomment the three `--` lines in step 3's query: a
   join that adds each branch's loan default rate, from `risk.loan_applications`, to the
   report's table. A reasonable-looking change to one query.
3. Start the worker and run it again. The changed query runs, and the run fails at
   **publish the report**: `PolicyRefused`, with the policy's reason, *This session has read
   restricted data, so its findings can't be published publicly*. Nothing was published.

The developer didn't touch the publish step, and the policy didn't look at the report. The
reporting job is entitled to read risk data (`branch-report-reads-risk` in `bank.dw`): it
builds internal reports from it, so the query was never going to be refused. What it isn't
entitled to do is publish from a run that read it. The gate recorded the query's result, which
says it read a table outside `analytics` (`restricted_data`), and the verifier refused the
publish because of what came before it in the run (`no-public-report-after-restricted-run-sql`).
A database permission can say whether a job may read a table; it can't say what the job may do
with the run once it has.

In the Temporal UI, the failed run's history shows it in order: `record
bank-analyst/run_sql/result`, then `verify bank-analyst/publish_report`, then the failure. The
successful run has the same steps, with a `publish_report` activity at the end.

## What's where

| File | What it is |
|---|---|
| `workflow.py` | **The workflow.** Four steps, each through the gate, and what the policy sees of a SQL step: the query's facts and one `read_table` check per table. Step 3's query holds the commented-out join. |
| `policy_gate.py` | **The gate.** `PolicyGate.execute(activity, *args)` asks the verifier over Nexus before running an activity, records its result with the verifier after, and raises `PolicyRefused`, failing the run, when the policy refuses. |
| `activities.py` | The data activities: `../queries.py`, under the agent's tool names; and `publish_report`, which publishes nothing. |
| `worker.py` | The worker. |

## Run

From `examples/governed_analytics`, with the stack from that README up (`just temporal`,
`just endpoint`, `just data`, `just verifier`), each in its own terminal:

```sh
just static-worker
just static-start       # runs the workflow and prints its result, or its failure
```

`just verifier` loads `bank.dw` when it starts: restart it after changing the policy.

## How it differs from the agent and the proxy

The agent's harness asks the same verifier the same questions before each tool call, and a
refusal goes back to the model. Here the workflow's own code asks, and a refusal fails the run.

Unlike the temporal-untrusted-workers proxy, which rejects a refused workflow task so the
workflow retries it forever, the gate is in the workflow: the question and the answer are in
its history, and a refusal fails the run with the policy's reason. The cost is that only code
that goes through the gate is checked.
