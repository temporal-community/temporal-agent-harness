# What the harness actually adds (and how it compares)

An honest account of the harness's net-new value over raw Temporal and over agent SDKs — useful
for positioning and for deciding when it's worth adopting.

## The four pillars, and which are inherited vs. invented

| Pillar | Inherited or invented? |
|---|---|
| **Durable execution** (crash-resume, retries, replay) | **Inherited** from Temporal — every agent is a workflow. |
| **Standardized event stream** | Transport **inherited** (`workflow_streams`); the *protocol + client-side merge* are **invented**. |
| **Human-in-the-loop approvals** | **Invented**, built on Temporal primitives (wait-conditions, updates). |
| **Composable typed subagents** | **Invented**, built on child workflows + a typed self-describing interface. |

The through-line: the harness does **not** add new distributed-systems primitives. Its net-new
value is **standardization** — a uniform contract over Temporal's primitives so a *heterogeneous
fleet of agents presents one face* to callers, operators, UIs, and supervising agents.

## The standardization is across three planes

Raw Temporal already standardizes **execution and invocation** (durable workflows; child-workflow /
Nexus calls). The harness adds standardization of three planes that raw composition leaves
bespoke-per-agent:

1. **Interface** — every agent takes the same `AgentConfig`, accepts the same `send_agent_message`
   envelope, advertises its operations via `agent_interface`, and handles multi-turn identically.
   → substitutability, runtime discovery, auto-generated calling glue (`subagent_toolset`).
2. **Observability** — one event protocol: a durable **AgentEvent history** (turns, model
   interactions, tool calls, approvals) on the `turn_events` topic, mergeable across the agent tree,
   live + replayable — the *same* for every agent regardless of SDK. → one UI, one analytics
   pipeline. See [The AgentEvent history is a durable, replayable stream](#the-agentevent-history-is-a-durable-replayable-stream).
3. **Control** — one approval-policy model + `tool_approval` surface, plus a standardized
   **operator-command (slash-command) surface** — `/approvals`, `/allow-tools`, `/status`, `/stop`
   out of the box (plus agent-added ones like `/model`), discoverable via `operator_interface` — all
   identical across agents. Operator commands run on their own channel (separate from the agent's `send_agent_message`
   handlers) and are audited as distinct control-plane events (`operator_command_*`, stamped
   `turn_number=0` so they never fold into agent turns). → one HITL **and operator** model for the
   whole fleet.

### The AgentEvent history is a durable, replayable stream

The Observability plane is carried by an **AgentEvent history** — a log of typed events (turns,
model interactions, the full tool lifecycle, approvals) that is *available as a stream*. This is
where "standardized event stream" (the second pillar above) becomes concrete:

- **It's a history, not just a live feed.** Every event is a Temporal Signal in the workflow's event
  history, so the log is durable, offset-addressed, and **reconstructed deterministically on replay**
  — the stream is a projection of Temporal's own history, not separate storage.
- **It's available as a replayable stream.** Consumers subscribe by `workflow_id` and read from an
  offset; a client that disconnects resumes without losing events. This is what backs the UI's
  play/pause replay — the UI holds no history of its own, so on restart it reattaches and rebuilds
  the whole stream from the workflow.
- **Transport inherited, protocol invented.** The stream *mechanism* is Temporal's
  `workflow_streams` (see the pillar table below); the harness's net-new is the uniform *AgentEvent
  vocabulary* and the client-side merge that presents a whole agent tree as one stream.

Full detail — the primitive, durability guarantees, `truncate` determinism on replay, retention
limits, and exactly how the harness wires it — is in
[`agentevent-workflow-stream.md`](agentevent-workflow-stream.md).

## vs. raw Temporal (child workflows / Nexus)

Raw "agents-as-workflows composed via child workflows or Nexus" already gives you heterogeneous,
durable, composable agents — and for cross-team/cross-namespace *transport*, **Nexus is arguably
better** than the harness's same-namespace child-workflow subagents. So composition is **not** the
harness's differentiator.

What the harness adds on top: the three planes above. In a raw setup each agent has a bespoke
interface, its own telemetry format, and its own (or no) HITL. The harness makes them uniform — at
the **cost** that every agent must conform to the harness contract (be built on it, or wrapped by an
integration). Worth it only if fleet-wide uniformity (one UI / one approval model / swap without
rewiring / runtime discovery) is valuable to you.

## vs. the OpenAI Agents SDK (bare)

Different *category*: the SDK is a **framework** (owns the agentic loop; batteries-included); the
harness is **infrastructure** (wraps whatever loop you write).

| Dimension | This harness | OpenAI Agents SDK (bare) |
|---|---|---|
| Layer | infrastructure (wraps a loop) | framework (owns the loop) |
| Execution | durable Temporal workflow | in-process, ephemeral |
| Crash-resume mid-turn | yes (inherited from Temporal) | not built-in |
| Durable HITL | yes, indefinite pause/resume | guardrails halt; DIY otherwise |
| Observability | durable, replayable event stream | tracing/telemetry |
| Maturity | experimental, v0.1 | shipped, mature |
| Time-to-first-agent / infra | higher (more code + Temporal infra) | low (pip, in-process) |
| Provider | multi-SDK by design | OpenAI-centric |

## vs. the OpenAI Agents SDK **+ Temporal integration**

This is the honest comparison, because `temporalio.contrib.openai_agents` runs the SDK's loop as a
Temporal workflow — which **neutralizes durability/execution/infra as differentiators** (both are
now durable workflows on Temporal). What remains for the harness:

- cross-SDK **standardization** (uniform contract regardless of which SDK wrote the loop);
- the agent-semantic **event stream** (Temporal history is durable but is *execution* history +
  tracing, not a curated, uniform, mergeable *agent* stream);
- first-class **HITL** (built-in policy engine vs. hand-rolled on signals).

And the intended relationship is **layered, not competitive**: the harness's OpenAI support is a
*vendored copy* of that very integration + streaming seams, and the roadmap is to run the SDK
(and others) *on* the harness. So the real question is "should my agent framework run on durable
execution *and* a standardized harness?", not "harness vs. SDK".

## Bottom line

The value **scales with heterogeneity and fleet size**:
- One OpenAI-only agent → marginal value over OpenAI+Temporal is modest (event stream + session/UI;
  HITL if the tool path is bridged).
- A mixed fleet (OpenAI + Gemini + script/Code-Mode agents) → the harness is the thing that makes
  them all interchangeable — same UI, observability, approvals, and composability.

Stated plainly: durability, composition, and durable HITL are Temporal primitives you *can* build
yourself; the harness's bet is that a **uniform contract + event stream + approval model across every
agent** is worth conforming each agent to one shape.
