// ABOUTME: Asserts every State flow card that stands for log rows resolves to the latest one
// from its OWN runtime — what a card's "view in logs" jumps to. Built from the same frames on both
// sides (buildAgentTreeGraph for the cards, buildReplayLog for the rows), so a rename on either
// surface that breaks the link fails here rather than as a button that silently does nothing.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildAgentTreeGraph } from "./flowProjection.ts";
import { latestLogRowForNode } from "./nodeLogRows.ts";
import { buildReplayLog } from "./replayLog.ts";

const frame = (event, data) => ({
  event,
  data: { type: event, agent_id: "root", turn_id: "t", timestamp: 1, ...data }
});

const parentFrames = [
  frame("message_accepted", {
    turn_number: 1,
    message_id: "m1",
    handler: "ask",
    payload: { text: "delegate" },
    disposition: "opened"
  }),
  frame("turn_started", { user_message: "delegate", turn_number: 1 }),
  frame("model_interaction_started", { turn_number: 1, model: "gpt-5.1" }),
  frame("tool_requested", { turn_number: 1, tool_id: "t1", tool_name: "search", tool_input: {} }),
  frame("tool_start", { turn_number: 1, tool_id: "t1", tool_name: "search" }),
  frame("tool_end", { turn_number: 1, tool_id: "t1", tool_name: "search", tool_output: "ok" }),
  frame("subagent_started", { turn_number: 1, subagent_id: "s1", workflow_id: "wf-a", agent_key: "task" }),
  frame("model_interaction_ended", { turn_number: 1, model: "gpt-5.1" }),
  frame("message_handler_end", { turn_number: 1, output: { text: "delegated" } })
];

const childFrames = [
  frame("turn_started", { agent_id: "s1", user_message: "summarize", turn_number: 1 }),
  frame("model_interaction_started", { agent_id: "s1", turn_number: 1, model: "gpt-5.1" }),
  frame("model_interaction_ended", { agent_id: "s1", turn_number: 1, model: "gpt-5.1" }),
  frame("message_handler_end", { agent_id: "s1", turn_number: 1, output: { text: "done" } })
];

const graph = buildAgentTreeGraph([
  { workflowId: "wf-parent", role: "parent", label: "parent", frames: parentFrames, agentInterface: [] },
  {
    workflowId: "wf-a",
    role: "subagent",
    label: "child",
    parentWorkflowId: "wf-parent",
    subagentId: "s1",
    agentKey: "task",
    frames: childFrames,
    agentInterface: []
  }
]);

const { rows } = buildReplayLog([
  ...parentFrames.map((f) => ({ frame: f, workflowId: "wf-parent", role: "parent" })),
  ...childFrames.map((f) => ({ frame: f, workflowId: "wf-a", role: "subagent" }))
]);

const rowFor = (nodeId) => latestLogRowForNode(nodeId, rows);

describe("a card's row in the logs", () => {
  it("is the latest row for that card, not the first", () => {
    assert.equal(rowFor("wf-parent::tool:t1")?.event, "tool_end");
    assert.equal(rowFor("wf-parent::model")?.event, "model_interaction_ended");
    assert.equal(rowFor("wf-parent::input")?.event, "turn_started");
  });

  it("comes from the card's own runtime", () => {
    const parentModel = rowFor("wf-parent::model");
    const childModel = rowFor("wf-a::model");
    assert.equal(parentModel?.workflowId, "wf-parent");
    assert.equal(childModel?.workflowId, "wf-a");
    assert.notEqual(parentModel?.id, childModel?.id);
  });

  it("is found for every card the graph draws that stands for log rows", () => {
    const cards = graph.nodes.filter((node) => node.type !== "agentWorkflow");
    const ids = new Set(cards.map((card) => card.id));
    for (const id of [
      "wf-parent::input",
      "wf-parent::model",
      "wf-parent::tool:t1",
      "wf-parent::tool-container",
      "wf-parent::output",
      "wf-a::input",
      "wf-a::model",
      "wf-a::output"
    ]) {
      assert.ok(ids.has(id), `the fixture should draw "${id}" (drew ${[...ids].join(", ")})`);
    }
    for (const card of cards) {
      assert.ok(rowFor(card.id), `card "${card.id}" has no row to view in the logs`);
    }
  });

  it("is null for a runtime frame, which stands for no single row", () => {
    assert.equal(rowFor("wf-parent::agent-runtime"), null);
  });
});
