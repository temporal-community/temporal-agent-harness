// ABOUTME: Asserts the runtime grid wraps at 3 columns and a tall card (including
// reasoning stacked under the model) does not land on the row below. The old
// layout walked nextX in one row, so a coding turn with a dozen tools was
// twelve cards wide.
//   node ui/scripts/check-runtime-wrap.mjs

import assert from "node:assert/strict";
import "./libAlias.mjs";

const { buildAgentGraph } = await import("../src/lib/state/flowProjection.ts");

const CARD = 230;
const GAP = 45;
const PAD = 40;
const THREE_COL = PAD * 2 + 3 * CARD + 2 * GAP;
const TWELVE_COL = PAD * 2 + 12 * CARD + 11 * GAP;

const frame = (event, data) => ({ event, data: { type: event, ...data } });

function run({ tools = 0, reasoning = false } = {}) {
  const toolFrames = Array.from({ length: tools }, (_, i) => [
    frame("tool_requested", {
      tool_id: `t${i}`,
      tool_name: "hub_write",
      tool_input: { i }
    }),
    frame("tool_end", { tool_id: `t${i}`, tool_name: "hub_write", tool_output: "ok" })
  ]).flat();
  return buildAgentGraph([
    frame("turn_started", { user_message: "code", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt" }),
    ...(reasoning
      ? [frame("thought_summary", { delta: { content: { text: "think" } } })]
      : []),
    ...toolFrames,
    frame("model_interaction_ended", { model: "gpt" }),
    frame("reply", { output: { text: "done" } })
  ]);
}

function node(graph, id) {
  const found = graph.nodes.find((item) => item.id === id);
  assert.ok(found, `missing node ${id}`);
  return found;
}

function toolsOf(graph) {
  return graph.nodes.filter((item) => item.id.startsWith("tool:"));
}

function heightOf(item) {
  if (item.id === "agent-runtime") return item.data.boundaryHeight;
  if (typeof item.data.nodeHeight === "number") return item.data.nodeHeight;
  return item.data.size === "large" ? 150 : 130;
}

{
  const graph = run({ tools: 11 });
  const flow = graph.nodes.filter(
    (item) => item.id === "model" || item.id.startsWith("tool:")
  );
  assert.equal(flow.length, 12, "model + 11 tools is the 12-slot grid");
  const xs = [...new Set(flow.map((item) => item.position.x))].sort((a, b) => a - b);
  const ys = [...new Set(flow.map((item) => item.position.y))].sort((a, b) => a - b);
  assert.equal(xs.length, 3, "12 runtime nodes wrap to 3 columns");
  assert.equal(ys.length, 4, "12 runtime nodes wrap to 4 rows");
  const boundary = node(graph, "agent-runtime").data.boundaryWidth;
  assert.ok(
    boundary <= THREE_COL + 30,
    `boundary ${boundary} should be ~3 cards (${THREE_COL}), not 12 (${TWELVE_COL})`
  );
  assert.ok(boundary < TWELVE_COL / 2, "boundary must not span a single 12-card row");
}

{
  const graph = run({ tools: 1 });
  const model = node(graph, "model");
  const tool = toolsOf(graph)[0];
  assert.ok(tool, "expected one tool");
  assert.equal(model.position.y, tool.position.y, "1–2 nodes stay on one row");
  const boundary = node(graph, "agent-runtime").data.boundaryWidth;
  assert.ok(boundary < THREE_COL, "two nodes must not reserve an empty third column");
}

{
  const graph = run({ tools: 12, reasoning: true });
  const model = node(graph, "model");
  const reasoning = node(graph, "reasoning");
  assert.equal(reasoning.position.x, model.position.x, "reasoning stays under the model");
  assert.ok(reasoning.position.y > model.position.y, "reasoning sits below the model");
  const below = toolsOf(graph)
    .filter((item) => item.position.x === model.position.x)
    .sort((a, b) => a.position.y - b.position.y)[0];
  assert.ok(below, "a wrapped tool should land in the model's column");
  const reasoningBottom = reasoning.position.y + heightOf(reasoning);
  assert.ok(
    below.position.y >= reasoningBottom,
    `row below overlaps reasoning: y=${below.position.y} reasoningBottom=${reasoningBottom}`
  );
}

{
  const graph = run({ tools: 12 });
  const input = node(graph, "input");
  const runtime = node(graph, "agent-runtime");
  const output = node(graph, "output");
  assert.ok(input.position.x < runtime.position.x, "input stays left of runtime");
  assert.ok(output.position.x > runtime.position.x, "output stays right of runtime");
  const wrapEdge = graph.edges.find(
    (edge) =>
      edge.sourceHandle === "source-bottom" &&
      edge.targetHandle === "target-top" &&
      edge.class === "edge-main"
  );
  assert.ok(wrapEdge, "wrapped rows should use the existing bottom→top edge handles");
}

{
  const err = "task_ask() takes 1 positional argument but 2 were given";
  const fails = Array.from({ length: 10 }, (_, i) => [
    frame("tool_requested", {
      tool_id: `t${i}`,
      tool_name: "task_ask",
      tool_input: { i }
    }),
    frame("tool_error", { tool_id: `t${i}`, tool_name: "task_ask", message: err })
  ]).flat();
  const graph = buildAgentGraph([
    frame("turn_started", { user_message: "go", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt" }),
    ...fails,
    frame("model_interaction_ended", { model: "gpt" })
  ]);
  const stacked = toolsOf(graph);
  assert.equal(stacked.length, 1, "10 identical failed task_ask collapse to one card");
  assert.equal(stacked[0].data.title, "task_ask");
  assert.equal(stacked[0].data.state, "failed ×10");
  assert.equal(stacked[0].data.detail, err);
}

{
  const graph = run({ tools: 3 });
  assert.equal(toolsOf(graph).length, 3, "successful distinct tools must not collapse");
}

{
  const graph = buildAgentGraph([
    frame("turn_started", { user_message: "go", turn_number: 1 }),
    frame("tool_requested", { tool_id: "a", tool_name: "task_ask", tool_input: {} }),
    frame("tool_error", { tool_id: "a", tool_name: "task_ask", message: "one" }),
    frame("tool_requested", { tool_id: "b", tool_name: "task_ask", tool_input: {} }),
    frame("tool_error", { tool_id: "b", tool_name: "task_ask", message: "two" })
  ]);
  assert.equal(toolsOf(graph).length, 2, "same name, different errors stay two cards");
}

{
  const graph = buildAgentGraph([
    frame("turn_started", { user_message: "go", turn_number: 1 }),
    frame("tool_requested", { tool_id: "a", tool_name: "task_ask", tool_input: {} }),
    frame("tool_error", { tool_id: "a", tool_name: "task_ask", message: "same" }),
    frame("tool_requested", { tool_id: "ok", tool_name: "bash", tool_input: {} }),
    frame("tool_end", { tool_id: "ok", tool_name: "bash", tool_output: "ok" }),
    frame("tool_requested", { tool_id: "c", tool_name: "task_ask", tool_input: {} }),
    frame("tool_error", { tool_id: "c", tool_name: "task_ask", message: "same" })
  ]);
  assert.equal(toolsOf(graph).length, 3, "non-consecutive identical failures stay separate");
}

{
  const graph = buildAgentGraph([
    frame("turn_started", { user_message: "go", turn_number: 1 }),
    frame("tool_requested", {
      tool_id: "ask",
      tool_name: "task_ask",
      tool_input: { question: "ok?" }
    }),
    frame("tool_approval_requested", {
      tool_id: "ask",
      tool_name: "task_ask",
      tool_input: { question: "ok?" }
    })
  ]);
  const tool = toolsOf(graph)[0];
  assert.ok(tool, "expected a task_ask card");
  assert.equal(tool.data.state, "awaiting");
  /* Chip uppercases the label next to the title on a 230px card. Two words
     ("AWAITING APPROVAL") overflowed the header; one word matches the
     already-shortened "requested" chip. */
  assert.ok(
    !/\s/.test(tool.data.state),
    `HITL chip label must be one word so uppercase fits the card: ${tool.data.state}`
  );
}

console.log(
  "check-runtime-wrap: 12 nodes → 3×4, boundary ~3 cards, reasoning under model, no row overlap; failed retries stack; HITL chip is one word"
);
