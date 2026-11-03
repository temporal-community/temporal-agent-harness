// ABOUTME: Asserts every State Flow node arrives with the size it was laid out at and the ports
// its edges use, and that each card fills that box. Without them xyflow hides every new node
// object until it has measured it in the DOM and draws no edge into it until it has found the
// handles there; the projection rebuilds every node on every frame, so that was every card,
// hidden and re-measured, on every streamed frame, and a reload painted nothing until it settled.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { buildAgentTreeGraph } from "$lib/state/flowProjection.ts";

const read = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
const frame = (event, data) => ({ event, data: { type: event, ...data } });
const tool = (event, id, name, extra = {}) => frame(event, { tool_id: id, tool_name: name, ...extra });

const parent = {
  workflowId: "wf-parent",
  role: "parent",
  label: "parent run",
  agentInterface: [{ name: "ask", description: "ask" }],
  frames: [
    frame("turn_started", { user_message: "go", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    frame("thought_summary", { delta: "thinking" }),
    tool("tool_requested", "host", "run_temporal_code", { tool_input: { script: "await a()\nawait b()\nawait c()" } }),
    tool("tool_start", "host", "run_temporal_code", { tool_input: { script: "await a()\nawait b()\nawait c()" } }),
    ...["a", "b", "c"].flatMap((id) => [
      tool("tool_requested", id, `call_${id}`, { tool_input: {} }),
      tool("tool_start", id, `call_${id}`, { tool_input: {} })
    ]),
    tool("tool_approval_requested", "c", "call_c", { tool_input: {} }),
    tool("auto_approval_evaluation_started", "c", "call_c", { evaluation_id: "e1", evaluator: "jev" }),
    frame("subagent_started", { subagent_id: "s1", workflow_id: "wf-a", agent_key: "task" })
  ]
};

const sub = {
  workflowId: "wf-a",
  role: "subagent",
  label: "child run",
  parentWorkflowId: "wf-parent",
  subagentId: "s1",
  agentKey: "task",
  agentInterface: [],
  frames: [
    frame("turn_started", { user_message: "summarize", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    frame("message_handler_end", { output: { text: "done" } })
  ]
};

const graph = buildAgentTreeGraph([parent, sub], { focus: false });
const reserved = (node) =>
  node.type === "agentWorkflow"
    ? { width: node.data.boundaryWidth, height: node.data.boundaryHeight }
    : {
        width: node.data.nodeWidth ?? (node.data.size === "large" ? 255 : 230),
        height: node.data.nodeHeight ?? (node.data.size === "large" ? 150 : 130)
      };

describe("known node sizes", () => {
  it("covers every kind of node the projection draws", () => {
    const ids = graph.nodes.map((node) => node.id);
    for (const expected of [
      "wf-parent::agent-runtime",
      "wf-parent::model",
      "wf-parent::reasoning",
      "wf-parent::tool:host",
      "wf-parent::tool:a",
      "wf-parent::approval:e1",
      "wf-parent::tool-container",
      "wf-a::agent-runtime",
      "wf-a::model"
    ]) {
      assert.ok(ids.includes(expected), `expected a node "${expected}" in ${ids.join(", ")}`);
    }
  });

  it("gives every node the width and height it was laid out at", () => {
    for (const node of graph.nodes) {
      const { width, height } = reserved(node);
      assert.equal(typeof width, "number", `${node.id} has no reserved width`);
      assert.equal(node.width, width, `${node.id} width`);
      assert.equal(node.height, height, `${node.id} height`);
    }
  });

  it("supplies every port an edge uses, on the side of the box it is drawn on", () => {
    const byId = new Map(graph.nodes.map((node) => [node.id, node]));
    assert.ok(graph.edges.length > 0);
    for (const edge of graph.edges) {
      for (const [end, handleId, type] of [
        [edge.source, edge.sourceHandle, "source"],
        [edge.target, edge.targetHandle, "target"]
      ]) {
        const node = byId.get(end);
        const handle = node?.handles?.find((item) => item.id === handleId && item.type === type);
        assert.ok(handle, `edge ${edge.id} uses ${type} handle ${handleId} that ${end} does not supply`);
        const along = { left: [0.5, null], right: [node.width - 0.5, null], top: [null, 0.5], bottom: [null, node.height - 0.5] };
        const [x, y] = along[handle.position];
        if (node.type === "agentState") {
          if (x != null) assert.equal(handle.x, x, `${node.id} ${handleId} x`);
          if (y != null) assert.equal(handle.y, y, `${node.id} ${handleId} y`);
        }
        assert.equal(handle.width, 0);
        assert.equal(handle.height, 0);
      }
    }
  });

  it("draws each card filling the node box, so its ports are where they were supplied", () => {
    const card = read("lib/components/flow/AgentStateNode.svelte");
    const boundary = read("lib/components/flow/AgentWorkflowNode.svelte");
    assert.match(card, /\.state-node \{[^}]*min-height: 100%;/);
    assert.doesNotMatch(card, /\.state-node\.large \{[^}]*min-height/);
    assert.match(boundary, /\.workflow-boundary \{[^}]*width: 100%;[^}]*height: 100%;/);
    assert.doesNotMatch(boundary, /style=\{`width:/);
  });
});
