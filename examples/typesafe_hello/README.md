# TypeSafe typed agent

Install with `pip install temporal-agent-harness`, or from this
checkout with `uv sync`. Set `TYPESAFE_API_KEY` and a Temporal
connection profile (`TEMPORAL_CONFIG_FILE`, `TEMPORAL_PROFILE`), then run:

```sh
uv run python -m examples.typesafe_hello.worker
```

Connect an `AgentClient` to a `TypeSafeHelloAgent` session and send the
`classify` handler a `Classify(text="...")` message. The reply retains the SDK's
native answers, confidence, served model and token usage plus the request ID.

Register the canonical `temporalio.typesafe.TypeSafePlugin` exactly once, before
`AgentHarnessPlugin`. Disable provider retries (`RetryPolicy(max_retries=0)`);
Temporal owns retries. `HarnessTypeSafe` mirrors `TemporalTypeSafe` call options
and requires the active session's runner. One model start/end span brackets the
whole durable call, including retries, and closes on failure or cancellation.
This is a non-streaming decision call; it produces no reply-text deltas.
Jev tool approvals use the same canonical provider activity. Their criteria,
thresholds and approval events remain owned by the harness; a workflow patch
retains the legacy approval activity for pre-migration histories.
