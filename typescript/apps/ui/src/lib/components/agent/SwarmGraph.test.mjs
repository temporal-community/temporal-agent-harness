// ABOUTME: Asserts what the swarm graph, scene and pane draw. Server renders (no DOM, no effects), so these
// check the markup: one element per member, shaped by role and marked when a person is needed, edges
// grouped by kind; and the pane's states before it has a swarm to show. The polling and the layout
// are covered by the swarm view tests and the browser check.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import SwarmGraph from "./SwarmGraph.svelte";
import SwarmPanel from "./SwarmPanel.svelte";
import SwarmScene from "./SwarmScene.svelte";
import { mockSwarmTopology } from "$lib/mock/swarmScenario.ts";
import { swarmGraph } from "$lib/state/swarmView.ts";

const topology = mockSwarmTopology("swarm-1", 0, { board: true });

describe("SwarmGraph", () => {
  it("draws every running member once, boards as squares, and marks those needing a person", () => {
    const graph = swarmGraph(topology);
    const { body } = render(SwarmGraph, { props: { graph } });
    const members = body.match(/data-member="/g) ?? [];
    assert.equal(members.length, graph.nodes.length);
    assert.equal((body.match(/<rect/g) ?? []).length, topology.lanes.length);
    const urgent = topology.nodes.filter((n) => !n.closed && n.phase === "awaiting_approval").length;
    assert.ok(urgent > 0, "the mock has a member waiting on an approval");
    assert.equal((body.match(/class="node[^"]*urgent/g) ?? []).length, urgent);
    for (const look of ["parent", "board", "peer", "lit"]) {
      assert.match(body, new RegExp(`class="edges ${look}[ "]`), `a path for ${look} edges`);
    }
  });

  it("labels the members that coordinate and leaves the workers unlabelled", () => {
    const { body } = render(SwarmGraph, { props: { graph: swarmGraph(topology) } });
    assert.ok(body.includes(">dispatcher</text>"));
    assert.ok(body.includes(">board · auth</text>"));
    assert.ok(!body.includes(">worker</text>"));
  });
});

describe("SwarmScene", () => {
  it("renders only its host on the server, where there is no WebGL to draw with", () => {
    const { body } = render(SwarmScene, { props: { graph: swarmGraph(topology), mode: "constellation" } });
    assert.match(body, /aria-label="Swarm in 3D/);
    assert.ok(!body.includes("<canvas"));
  });
});

describe("SwarmPanel", () => {
  const never = () => new Promise(() => {});

  it("says so when no session is selected", () => {
    const { body } = render(SwarmPanel, {
      props: { sessionId: null, loadMembership: never, loadTopology: never }
    });
    assert.ok(body.includes("No session selected"));
  });

  it("reads before it draws", () => {
    const { body } = render(SwarmPanel, {
      props: { sessionId: "wf-1", loadMembership: never, loadTopology: never }
    });
    assert.ok(body.includes("Reading the swarm"));
  });
});
