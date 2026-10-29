// ABOUTME: Asserts that the parent runtime boundary does not repeat the agent name the console
// header already shows, while keeping its own description and a subagent's label.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildAgentTreeGraph } from "$lib/state/flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });

describe("the parent runtime subtitle", () => {
  it("omits the agent name the header already shows", () => {
    const graph = buildAgentTreeGraph([
      {
        workflowId: "wf-parent",
        role: "parent",
        label: "Billing Concierge",
        frames: [
          frame("turn_started", { user_message: "delegate", turn_number: 1 }),
          frame("subagent_started", { subagent_id: "s1", workflow_id: "wf-a", agent_key: "task" })
        ],
        agentInterface: []
      },
      {
        workflowId: "wf-a",
        role: "subagent",
        label: "Refund checker",
        parentWorkflowId: "wf-parent",
        subagentId: "s1",
        agentKey: "task",
        frames: [],
        agentInterface: []
      }
    ]);
    const boundary = (id) => graph.nodes.find((node) => node.id === `${id}::agent-runtime`).data;

    const parent = boundary("wf-parent");
    assert.equal(parent.title, "Parent agent runtime");
    assert.doesNotMatch(parent.subtitle, /Billing Concierge/);
    assert.equal(parent.subtitle, "model calls, tool execution, and internal state");
    assert.match(boundary("wf-a").subtitle, /Refund checker/);
  });
});
