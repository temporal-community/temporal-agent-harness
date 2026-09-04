// ABOUTME: Asserts that a subagent's own flow arrows are drawn AND can be seen. The projection has
// always emitted them — `wf-a::flow-input-model` and `wf-a::flow-model-output` between a subagent's
// Input, Model interaction and Output — but they were invisible in the canvas, which reads exactly
// like a subagent whose interior was deliberately left unconnected. Two separate things buried them,
// so both are pinned here: a stacking order that put an embedded graph's edges under the very card
// they are embedded in, and a `z-index` on `.svelte-flow__edges` that flattened every edge in the
// canvas onto one rung and made per-edge z-index meaningless. Measured in a browser: with either one
// back, `document.elementFromPoint` at a subagent edge's own midpoint returns the Subagent activity
// container rather than the edge.
//   node ui/scripts/check-subagent-flow-edges.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import "./libAlias.mjs";

/* Dynamic, because a static import is resolved before the alias hook above has
   run. Same reason the other checks in here reach for their modules this way. */
const { buildAgentTreeGraph } = await import("../src/lib/state/flowProjection.ts");

const frame = (event, data) => ({ event, data: { type: event, ...data } });

const parent = {
  workflowId: "wf-parent",
  role: "parent",
  label: "parent run",
  frames: [
    frame("turn_started", { user_message: "delegate", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    frame("subagent_started", { subagent_id: "s1", workflow_id: "wf-a", agent_key: "task" }),
    frame("reply", { output: { text: "delegated" } })
  ],
  agentInterface: []
};

const sub = {
  workflowId: "wf-a",
  role: "subagent",
  label: "child run",
  parentWorkflowId: "wf-parent",
  subagentId: "s1",
  agentKey: "task",
  frames: [
    frame("turn_started", { user_message: "summarize the README", turn_number: 2 }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    frame("model_interaction_ended", { model: "gpt-5.1" }),
    frame("reply", { output: { text: "one concise paragraph" } }),
    frame("turn_end", { turn_number: 2 })
  ],
  agentInterface: [{ name: "ask", description: "ask the subagent something" }],
  stopped: true
};

const graph = buildAgentTreeGraph([parent, sub]);
const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
const zOf = (id) => {
  const node = nodeById.get(id);
  assert.ok(node, `expected a node "${id}"`);
  return node.zIndex ?? 0;
};

/* The flow itself, before anything about how it paints: a subagent that took a
   turn runs Input -> Model interaction -> Output, and the graph should say so. */
const subagentEdges = graph.edges.filter((edge) => edge.id.startsWith("wf-a::"));
assert.deepEqual(
  subagentEdges.map((edge) => `${edge.source} -> ${edge.target}`).sort(),
  ["wf-a::input -> wf-a::model", "wf-a::model -> wf-a::output"],
  "a subagent that ran a turn should draw the same Input -> Model -> Output chain its parent does"
);

/* A rewritten node id that no edge follows is the other way this fails, and it
   looks identical on screen: scopedGraph prefixes both, so they must still meet. */
for (const edge of graph.edges) {
  assert.ok(nodeById.has(edge.source), `edge "${edge.id}" dangles: no node "${edge.source}"`);
  assert.ok(nodeById.has(edge.target), `edge "${edge.id}" dangles: no node "${edge.target}"`);
}

/* The ladder each subagent edge has to sit on. Above the Subagent activity
   container it is drawn inside — the rung that was missing, since the container
   is a card at 10 and the edges were lifted only to 5 — and above the subagent's
   own boundary, whose gradient is near-opaque. Below the two cards it runs
   between, so it passes behind them rather than across their faces. */
const containerZ = zOf("wf-parent::tool-container");
const boundaryZ = zOf("wf-a::agent-runtime");
for (const edge of subagentEdges) {
  const z = edge.zIndex ?? 0;
  assert.ok(
    z > containerZ,
    `edge "${edge.id}" at z ${z} is under the Subagent activity container at ${containerZ}`
  );
  assert.ok(
    z > boundaryZ,
    `edge "${edge.id}" at z ${z} is under the subagent boundary at ${boundaryZ}`
  );
  for (const end of [edge.source, edge.target]) {
    assert.ok(
      z < zOf(end),
      `edge "${edge.id}" at z ${z} paints over the card "${end}" at ${zOf(end)}`
    );
  }
}

/* Lifting the nodes and leaving the edges behind is precisely the bug, so the
   parent's own ladder has to keep its shape too — an edge that cleared the
   container by also clearing its endpoints would have satisfied the loop above. */
for (const edge of graph.edges.filter((item) => item.id.startsWith("wf-parent::"))) {
  const z = edge.zIndex ?? 0;
  assert.ok(
    z > zOf("wf-parent::agent-runtime"),
    `parent edge "${edge.id}" at z ${z} is under its own boundary`
  );
  assert.ok(z < zOf(edge.source), `parent edge "${edge.id}" paints over "${edge.source}"`);
}

/**
 * And the CSS half, which no projection assertion can reach.
 *
 * `.svelte-flow__edges` is positioned absolutely by the library, so any z-index
 * here opens a stacking context around every edge in the canvas: each one then
 * paints at that single number regardless of its own, and the ordering asserted
 * above stops meaning anything on screen. Its sibling `.svelte-flow__nodes` is
 * static and may keep whatever it likes, because z-index on a static box does
 * nothing — which is why the pair used to read as symmetric and was not.
 */
const flowSource = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "../src/lib/components/flow/AgentStateFlow.svelte"),
  "utf8"
);
const edgesLayerRule = flowSource.match(/\.svelte-flow__edges\)\s*\{([^}]*)\}/);
assert.ok(
  !edgesLayerRule || !/z-index/.test(edgesLayerRule[1]),
  "a z-index on .svelte-flow__edges flattens every edge onto one rung — " +
    `found: ${edgesLayerRule?.[1].trim()}`
);

console.log("check-subagent-flow-edges: ok");
