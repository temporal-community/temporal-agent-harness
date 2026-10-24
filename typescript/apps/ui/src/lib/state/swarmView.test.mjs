// ABOUTME: Tests for the swarm pane's reading of a topology: which members and edges are drawn,
// how they look, the layout key that ignores phases and peer edges, and the counts beside it.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { needsAttention, radiusOf, shortId, swarmGraph, swarmShape, swarmSummary } from "./swarmView.ts";

const node = (id, role, extra = {}) => ({
  workflow_id: id,
  workflow_type: "T",
  role,
  parent_id: null,
  phase: "working",
  status: "RUNNING",
  closed: false,
  start_time: 1,
  close_time: null,
  last_active: 1,
  ...extra
});

const edge = (source, target, kind, weight = 1) => ({ source, target, kind, weight });

const topology = (extra = {}) => ({
  swarm_id: "s",
  as_of: 10,
  truncated: false,
  nodes: [
    node("d", "dispatcher", { phase: "awaiting_agent" }),
    node("lead-1", "lead", { parent_id: "d", phase: "awaiting_agent" }),
    node("w-1", "worker", { parent_id: "lead-1", phase: "awaiting_approval", last_active: 5 }),
    node("w-2", "worker", { parent_id: "lead-1", phase: "awaiting_approval", last_active: 3 }),
    node("w-old", "worker", { parent_id: "lead-1", closed: true, status: "COMPLETED", phase: "idle" }),
    node("sp", "specialist", { parent_id: "d", phase: "idle" }),
    node("swarm-board-s-auth", "board", { phase: null })
  ],
  edges: [
    edge("d", "lead-1", "parent"),
    edge("lead-1", "w-1", "parent"),
    edge("lead-1", "w-2", "parent"),
    edge("lead-1", "w-old", "parent"),
    edge("d", "sp", "parent"),
    edge("w-1", "sp", "peer"),
    edge("w-old", "swarm-board-s-auth", "board", 3)
  ],
  lanes: [{ workflow_id: "swarm-board-s-auth", lane: "auth", counts: { open: 2, claimed: 2, done: 5 } }],
  phases: { awaiting_agent: 2, awaiting_approval: 2, idle: 1, none: 1 },
  roles: { worker: 3, dispatcher: 1, lead: 1, specialist: 1, board: 1 },
  ...extra
});

describe("swarmGraph", () => {
  it("drops closed members and their edges unless asked to keep them", () => {
    const live = swarmGraph(topology());
    assert.ok(!live.nodes.some((n) => n.id === "w-old"));
    assert.ok(!live.edges.some((e) => e.source === "w-old" || e.target === "w-old"));

    const all = swarmGraph(topology(), { showClosed: true });
    const old = all.nodes.find((n) => n.id === "w-old");
    assert.equal(old.tone, "--text-4");
    assert.ok(all.edges.some((e) => e.id === "board:w-old>swarm-board-s-auth" && e.weight === 3));
  });

  it("colors by phase, sizes by role, and labels a lane by its id", () => {
    const { nodes } = swarmGraph(topology());
    const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
    assert.equal(byId["w-1"].tone, "--live");
    assert.equal(byId["d"].tone, "--reasoning");
    assert.equal(byId["swarm-board-s-auth"].tone, "--accent");
    assert.equal(byId["swarm-board-s-auth"].label, "board · auth");
    assert.ok(byId["d"].radius > byId["lead-1"].radius && byId["lead-1"].radius > byId["w-1"].radius);
    assert.equal(radiusOf(null), radiusOf("someone-new"));
  });
});

describe("swarmShape", () => {
  it("ignores phases and peer edges, but not members or structural edges", () => {
    const base = swarmShape(swarmGraph(topology()));
    const recolored = topology();
    recolored.nodes = recolored.nodes.map((n) => ({ ...n, phase: "idle" }));
    recolored.edges = recolored.edges.filter((e) => e.kind !== "peer");
    assert.equal(swarmShape(swarmGraph(recolored)), base);

    const grown = topology();
    grown.nodes.push(node("w-3", "worker", { parent_id: "lead-1" }));
    assert.notEqual(swarmShape(swarmGraph(grown)), base);
  });
});

describe("swarmSummary", () => {
  it("counts members, phases in legend order, roles most first, and the board", () => {
    const summary = swarmSummary(topology());
    assert.deepEqual([summary.running, summary.closed], [6, 1]);
    assert.deepEqual(
      summary.phases.map((p) => p.phase),
      ["awaiting_approval", "awaiting_agent", "idle"]
    );
    assert.deepEqual(summary.roles[0], { role: "worker", count: 3 });
    assert.deepEqual(summary.board, { open: 2, claimed: 2, done: 5 });
    assert.equal(summary.peerEdges, 1);
    assert.equal(swarmSummary(topology({ lanes: [] })).board, null);
  });
});

describe("needsAttention", () => {
  it("lists the members blocked on a person, longest waiting first", () => {
    assert.deepEqual(
      needsAttention(topology()).map((n) => n.workflow_id),
      ["w-2", "w-1"]
    );
  });
});

describe("shortId", () => {
  it("keeps a workflow id's last segment", () => {
    assert.equal(shortId("swarm-board-s-auth"), "auth");
    assert.equal(shortId("worker-subagent-0123456789abcdef"), "0123456789ab");
  });
});
