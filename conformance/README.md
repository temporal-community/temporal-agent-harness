# Conformance

Scenarios every harness SDK's worker is run through, and the `turn_events` streams Python's
worker publishes for them. An SDK conforms when its streams, normalized as below, equal these.
The event types themselves are in [`contract/protocol.schema.json`](../contract/protocol.schema.json).

| Directory | What it holds |
| --- | --- |
| `scenarios/` | One JSON file per scenario: the messages a client sends and what it does when given events arrive. |
| `goldens/` | Per scenario, `<name>.events.jsonl` (every agent's stream, normalized) and `<name>.sends.jsonl` (what each send returned). Recorded from Python. |

After a change to the harness that alters a stream, or to a scenario, record them again and
commit the result:

```bash
bazel run //python/tests/conformance:goldens_update
```

`bazel test //python/tests/conformance:test_goldens` fails while a recording differs from its
golden.

## How a scenario runs

Two test-only programs stand in for what surrounds the harness core in a real session:

- **The scripted integration** (`python/tests/conformance/scripted_agent.py`) is the agent's inner
  loop, in place of a model and its AI SDK. Each message carries a script, and the agent plays its
  steps in order. Its three handlers, `run_reject`, `run_enqueue` and `run_accept`, differ only in
  what a message does when it arrives mid-turn.
- **The driver** (`python/tests/conformance/driver.py`) is the client. It starts a local Temporal
  server (`temporal server start-dev`) and a worker, sends the scenario's messages, waits for the
  events it names, approves tools and answers callbacks, and records each agent's stream.

The integration publishes only what a model integration would, and only what the script states:

| Step | Publishes, then calls |
| --- | --- |
| `model` | `model_interaction_started`, a `reply_delta` per `reply` chunk, `model_interaction_ended` with `usage` |
| `tool` | `tool_requested`, then `run_tool` with the step's `id` |
| `state_set`, `state_delete` | nothing; one `mutate()` of the agent's `entries` state |
| `subagent_start`, `subagent_send`, `subagent_stop` | nothing; the runner's subagent calls |
| `raise` | nothing; the handler raises `message` and later steps never run |

Its tools are `add` (an activity tool), `echo` (a workflow tool), `fail` (raises), `gated` (needs
approval) and `ask_client` (a callback tool). A failed tool call or subagent turn does not end the
handler, as in a model loop.

Before any golden comparison, the test derives from each message's script the events the
integration must publish and compares them with the recorded stream. A mismatch there means the
integration diverged from its script, so a golden difference is always in events the core
publishes.

Handlers that run at the same time are sequenced by the scenario, not by a scheduler: a handler
waits on `ask_client` until the driver answers it, and the driver acts only after it sees the
event it is waiting for. Only one handler can run at any moment, so a stream's order is fixed.

A stopped subagent's stream cannot be read, so a scenario that stops one first waits for the
subagent's last `turn_end`.

## Normalization

- Every id the harness mints becomes `<kind>-<n>`, numbered by first appearance across the
  scenario (the root agent's stream, then each subagent's in start order, then the sends):
  `agent_id` and `subagent_id` (`agent-`), `turn_id` (`turn-`), `message_id` (`message-`),
  `workflow_id` (`workflow-`), `evaluation_id` (`evaluation-`). Tool ids come from the script and
  stay, and so does the empty `turn_id` of an event published outside a turn (a state snapshot
  at registration, `turn_number` 0).
- `timestamp` is dropped, after checking it never decreases within a stream.
- Everything else stays, including `turn_number` and `from_offset`.
- One JSON object per line, keys sorted.
