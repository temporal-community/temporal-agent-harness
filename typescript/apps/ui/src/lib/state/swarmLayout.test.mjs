// ABOUTME: Tests for where the 3D swarm view puts members: the plane keeps members where they were
// and starts new ones beside their parent; constellation stacks the hierarchy in tiers with board
// lanes between the last two; growth stacks by start time; and the edge and particle buffers.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { mockSwarmTopology } from "../mock/swarmScenario.ts";
import { PlanarLayout, depths, ease, peerParticles, phaseOf, segments, targets } from "./swarmLayout.ts";
import { swarmGraph } from "./swarmView.ts";

const graphAt = (read) => swarmGraph(mockSwarmTopology("swarm-1", read, { board: true }));

const settled = (graph) => {
  const layout = new PlanarLayout();
  layout.update(graph);
  for (let i = 0; i < 400 && layout.tick(); i++);
  return layout;
};

describe("PlanarLayout", () => {
  it("settles, and keeps a settled member put when only phases change", () => {
    const graph = graphAt(0);
    const layout = settled(graph);
    assert.ok(layout.settled);
    const before = layout.position("lead-auth");
    const recolored = { ...graph, nodes: graph.nodes.map((n) => ({ ...n, phase: "idle" })) };
    assert.equal(layout.update(recolored), false, "same shape: no reheat");
    assert.equal(layout.tick(), false);
    assert.deepEqual(layout.position("lead-auth"), before);
  });

  it("starts a new member beside its parent", () => {
    const graph = graphAt(0);
    const layout = settled(graph);
    const lead = layout.position("lead-auth");
    const extra = { ...graph.nodes.find((n) => n.role === "worker" && n.node.parent_id === "lead-auth") };
    extra.id = "worker-new";
    extra.node = { ...extra.node, workflow_id: "worker-new" };
    assert.equal(layout.update({ ...graph, nodes: [...graph.nodes, extra] }), true);
    const placed = layout.position("worker-new");
    assert.ok(Math.hypot(placed.x - lead.x, placed.y - lead.y) <= 21);
  });
});

describe("targets", () => {
  const graph = graphAt(0);
  const layout = settled(graph);

  it("stacks constellation tiers by depth, with lanes between the last two", () => {
    const depthOf = depths(graph);
    assert.deepEqual(
      [depthOf.get("swarm-1"), depthOf.get("lead-auth"), depthOf.get("swarm-board-swarm-1-auth")],
      [0, 1, 0]
    );
    const at = targets(graph, layout, "constellation", { tier: 10 });
    const up = (id) => at.get(id)[1];
    const worker = graph.nodes.find((n) => n.role === "worker").id;
    assert.deepEqual([up("swarm-1"), up("lead-auth"), up(worker)], [10, 0, -10]);
    assert.equal(up("swarm-board-swarm-1-auth"), -5);
    const plane = layout.position("lead-auth");
    assert.deepEqual([at.get("lead-auth")[0], at.get("lead-auth")[2]], [plane.x, plane.y]);
  });

  it("spreads growth over start times, earliest lowest", () => {
    const g = {
      ...graph,
      nodes: graph.nodes.map((n, i) => ({ ...n, node: { ...n.node, start_time: i } }))
    };
    const at = targets(g, settled(g), "growth", { height: 100 });
    const ups = g.nodes.map((n) => at.get(n.id)[1]);
    assert.equal(ups[0], -50);
    assert.equal(ups.at(-1), 50);
    assert.ok(ups.every((u, i) => i === 0 || u >= ups[i - 1]));
  });
});

describe("buffers", () => {
  const at = new Map([
    ["a", [0, 0, 0]],
    ["b", [10, 0, 0]],
    ["c", [0, 10, 0]]
  ]);
  const edges = [
    { id: "parent:a>b", source: "a", target: "b", kind: "parent", weight: 1 },
    { id: "peer:a>c", source: "a", target: "c", kind: "peer", weight: 1 },
    { id: "peer:a>gone", source: "a", target: "gone", kind: "peer", weight: 1 }
  ];

  it("writes one segment per placed edge of the chosen kinds", () => {
    assert.deepEqual([...segments(edges, at, new Set(["parent"]))], [0, 0, 0, 10, 0, 0]);
    assert.equal(segments(edges, at, new Set(["parent", "peer"])).length, 12);
  });

  it("runs particles from the asker toward the peer, wrapping each period", () => {
    const one = (t) => [...peerParticles(edges, at, t, { perEdge: 1, period: 1 })];
    const start = one(0);
    assert.equal(start.length, 3);
    const offset = phaseOf("peer:a>c");
    assert.ok(Math.abs(start[1] - offset * 10) < 1e-4, "along a>c, from a");
    assert.deepEqual(one(1).map((v) => Math.round(v * 1e4)), start.map((v) => Math.round(v * 1e4)));
  });

  it("eases toward targets, drops departed members and places new ones at once", () => {
    const current = new Map([["a", [0, 0, 0]], ["old", [1, 1, 1]]]);
    ease(current, new Map([["a", [10, 0, 0]], ["new", [5, 5, 5]]]), 0.5);
    assert.deepEqual([...current.keys()].sort(), ["a", "new"]);
    assert.deepEqual(current.get("a"), [5, 0, 0]);
    assert.deepEqual(current.get("new"), [5, 5, 5]);
  });
});
