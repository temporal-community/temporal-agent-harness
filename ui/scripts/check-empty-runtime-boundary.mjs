// ABOUTME: Asserts that a runtime boundary with nothing inside it reserves no room for cards it does
// not have. runtimeLayoutFor seeds contentWidth/contentHeight with one nominal card before its loop
// runs, so a subagent that contributed NO nodes used to reserve room for one: measured in a browser,
// a 310x290 boundary around an 88px header, 202px of nothing under a stopped subagent whose own
// stream was never readable. Both directions are checked here — that an empty boundary collapses to
// exactly the header its component declares, and that a populated one still holds its cards — because
// a fix that only shrinks would be satisfied by collapsing every boundary onto its contents.
//   node ui/scripts/check-empty-runtime-boundary.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compile } from "svelte/compiler";
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
    frame("subagent_started", { subagent_id: "s1", workflow_id: "wf-a", agent_key: "task" }),
    frame("subagent_started", { subagent_id: "s2", workflow_id: "wf-b", agent_key: "task" })
  ],
  agentInterface: []
};

/* A child that streamed something of its own, so its boundary has cards in it. */
const busyFrames = [
  frame("turn_started", { user_message: "do the thing", turn_number: 1 }),
  frame("model_interaction_started", { model: "gpt-5.1" }),
  frame("reply", { output: { text: "done" } })
];

const sub = (workflowId, subagentId, frames, stopped = true) => ({
  workflowId,
  role: "subagent",
  label: "child run",
  parentWorkflowId: "wf-parent",
  subagentId,
  agentKey: "task",
  frames,
  agentInterface: [{ name: "ask", description: "ask the subagent something" }],
  stopped
});

const stateNodeWidth = 230;
const largeStateNodeWidth = 255;
const stateNodeHeight = 130;
const largeStateNodeHeight = 150;

/** The box the layout reserved for a node, whichever kind of node it is. */
function boxOf(node) {
  const width =
    node.type === "agentWorkflow"
      ? node.data.boundaryWidth
      : node.data.nodeWidth ??
        (node.data.size === "large" ? largeStateNodeWidth : stateNodeWidth);
  const height =
    node.type === "agentWorkflow"
      ? node.data.boundaryHeight
      : node.data.nodeHeight ??
        (node.data.size === "large" ? largeStateNodeHeight : stateNodeHeight);
  assert.ok(
    typeof width === "number" && typeof height === "number",
    `node "${node.id}" reserves no box: ${JSON.stringify({ width, height })}`
  );
  return {
    id: node.id,
    left: node.position.x,
    top: node.position.y,
    right: node.position.x + width,
    bottom: node.position.y + height,
    width,
    height
  };
}

const contains = (outer, inner) =>
  inner.left >= outer.left &&
  inner.right <= outer.right &&
  inner.top >= outer.top &&
  inner.bottom <= outer.bottom;

const boundaryOf = (graph, workflowId) =>
  boxOf(graph.nodes.find((node) => node.id === `${workflowId}::agent-runtime`));

/** Every node of one workflow except its own boundary — the cards it drew. */
const cardsOf = (graph, workflowId) =>
  graph.nodes
    .filter(
      (node) => node.id.startsWith(`${workflowId}::`) && node.id !== `${workflowId}::agent-runtime`
    )
    .map(boxOf);

// --- what the component says its header measures ----------------------------
/* The collapsed height is not a number this check is free to choose: it is the
   header's own box, and flowProjection can only mirror it because these positions
   are computed from data and never measured from the DOM. Reading it out of the
   component's compiled CSS is what keeps the mirror honest — a min-height edited
   there and not here would otherwise let the header paint outside its boundary. */
const headerHeight = (() => {
  const source = readFileSync(
    new URL("../src/lib/components/flow/AgentWorkflowNode.svelte", import.meta.url),
    "utf8"
  );
  const { css } = compile(source, { generate: "client", css: "external" });
  const code = css?.code ?? "";
  const declared = [...code.matchAll(/([^{}]*\.workflow-head[^{}]*)\{([^}]*)\}/g)]
    .flatMap((block) => [...block[2].matchAll(/(?:^|;)\s*min-height\s*:\s*([^;]*)/g)])
    .map((match) => match[1].trim());
  assert.deepEqual(
    declared.length,
    1,
    `.workflow-head should declare exactly one min-height, found ${JSON.stringify(declared)}`
  );
  const pixels = /^(\d+(?:\.\d+)?)px$/.exec(declared[0]);
  assert.ok(pixels, `.workflow-head min-height "${declared[0]}" is not a pixel length`);
  return Number(pixels[1]);
})();

// --- a frameless, stopped subagent is its header and nothing else -----------
{
  const graph = buildAgentTreeGraph([parent, sub("wf-a", "s1", []), sub("wf-b", "s2", [])]);

  for (const workflowId of ["wf-a", "wf-b"]) {
    assert.deepEqual(
      cardsOf(graph, workflowId),
      [],
      `${workflowId} drew no cards, so there is nothing for its boundary to hold`
    );
    assert.deepEqual(
      boundaryOf(graph, workflowId).height,
      headerHeight,
      `a frameless subagent's boundary must collapse to its header (${headerHeight}px), ` +
        "not reserve room for a card it does not have"
    );
  }

  /* Stacked, they neither collide nor keep the old gap: exactly the same spacing
     the container puts between two populated children. */
  const [first, second] = ["wf-a", "wf-b"].map((id) => boundaryOf(graph, id));
  assert.ok(second.top >= first.bottom, "two collapsed boundaries must not overlap");
  assert.deepEqual(second.top - first.bottom, 32, "and must sit at the container's own gap");

  /* The chain the reservation feeds. Containment rather than arithmetic: a
     boundary height that is wrong by any amount shows up as a child hanging out
     of the box that was sized to hold it. */
  const container = boxOf(graph.nodes.find((node) => node.id === "wf-parent::tool-container"));
  const parentBoundary = boundaryOf(graph, "wf-parent");
  for (const child of [first, second]) {
    assert.ok(contains(container, child), `${child.id} hangs out of the Subagent activity card`);
  }
  assert.ok(contains(parentBoundary, container), "the container hangs out of the parent boundary");
}

// --- a subagent that DID stream keeps every card it drew ---------------------
// Otherwise "collapse the empty ones" is satisfied by collapsing all of them.
{
  const graph = buildAgentTreeGraph([parent, sub("wf-a", "s1", busyFrames)]);
  const boundary = boundaryOf(graph, "wf-a");
  const cards = cardsOf(graph, "wf-a");

  assert.ok(cards.length > 0, "the fixture should give the child cards of its own");
  assert.ok(
    boundary.height > headerHeight,
    "a populated boundary must be taller than its header"
  );
  for (const card of cards) {
    assert.ok(contains(boundary, card), `${card.id} hangs out of the boundary that should hold it`);
  }
}

// --- mixed siblings: one empty, one populated ------------------------------
// The realistic shape, and where a wrong height misaligns a sibling rather than
// just leaving a gap. Checked in both orders, since the empty one being first is
// what shifts everything below it.
for (const [emptyId, busyId] of [
  ["wf-a", "wf-b"],
  ["wf-b", "wf-a"]
]) {
  const agents = [parent, sub("wf-a", "s1", emptyId === "wf-a" ? [] : busyFrames),
    sub("wf-b", "s2", emptyId === "wf-b" ? [] : busyFrames)];
  const graph = buildAgentTreeGraph(agents);
  const empty = boundaryOf(graph, emptyId);
  const busy = boundaryOf(graph, busyId);
  const container = boxOf(graph.nodes.find((node) => node.id === "wf-parent::tool-container"));

  assert.deepEqual(
    empty.height,
    headerHeight,
    `${emptyId} is empty beside a populated sibling and must still collapse`
  );
  assert.ok(
    empty.bottom <= busy.top || busy.bottom <= empty.top,
    "the siblings must not overlap each other"
  );
  for (const card of cardsOf(graph, busyId)) {
    assert.ok(
      contains(busy, card),
      `${card.id} misaligned against its boundary beside a collapsed sibling`
    );
  }
  for (const child of [empty, busy]) {
    assert.ok(contains(container, child), `${child.id} hangs out of the Subagent activity card`);
  }
  assert.ok(
    contains(boundaryOf(graph, "wf-parent"), container),
    "the container hangs out of the parent boundary"
  );
}

// --- empty is empty, not empty YET ------------------------------------------
/* A child enters the graph the moment the parent announces it, and `frames` is
   scoped to the replay cursor — so "announced, nothing of its own yet" is a
   position anyone scrubbing the run parks on, not a flicker at mount. Collapsing
   there would drop the card and then jump it when the next frame arrives. */
{
  const running = buildAgentTreeGraph([parent, sub("wf-a", "s1", [], false)]);
  const stopped = buildAgentTreeGraph([parent, sub("wf-a", "s1", [], true)]);

  assert.deepEqual(cardsOf(running, "wf-a"), [], "the fixture should leave the child frameless");
  assert.ok(
    boundaryOf(running, "wf-a").height > boundaryOf(stopped, "wf-a").height,
    "a running subagent with nothing in it yet must keep the room its cards are coming to"
  );
  assert.ok(
    boundaryOf(running, "wf-a").height >= headerHeight + stateNodeHeight,
    "and that room is a card's worth"
  );
}

console.log(
  `check-empty-runtime-boundary: an empty boundary is its ${headerHeight}px header, ` +
    "a populated one holds its cards, and a running one keeps its reservation"
);
