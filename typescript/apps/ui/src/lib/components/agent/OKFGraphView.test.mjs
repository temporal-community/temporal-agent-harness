// ABOUTME: The OKF graph draws once and moves its elements per tick, so two things can rot silently.
// Each edge has to end at ITS OWN target's rim, looked up by id: the per-edge scan of every node
// this replaced was most of a 10,000-node frame. And a re-walk that found the same concepts and
// links must not count as a new layout, or every state change on the mount shakes a settled graph.
// A link made twice is drawn once: two edges with one key threw, and the graph drew nothing.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import OKFGraphView from "./OKFGraphView.svelte";
import { graphElements, graphShape, typeColors } from "$lib/state/okfGraph.ts";

const concept = (id, size) => ({
  id,
  path: `${id}.md`,
  type: "Person",
  title: id,
  description: "",
  tags: [],
  status: "stable",
  trust_tier: "unverified",
  stale: false,
  size,
  problem: null
});

const graph = {
  concepts: [concept("small", 0), concept("big", 8_100)],
  links: [
    { source: "small", target: "big", dangling: false },
    { source: "big", target: "small", dangling: false },
    { source: "big", target: "small", dangling: false },
    { source: "small", target: "missing/ghost", dangling: true }
  ],
  truncated: false,
  walked_at: 0
};

describe("the OKF graph view", () => {
  it("ends every edge at its own target's rim", () => {
    const html = render(OKFGraphView, { props: { graph, colors: typeColors(graph.concepts) } }).body;
    const at = new Map(
      [...html.matchAll(/data-concept="([^"]+)" transform="translate\(([-\d.e]+) ([-\d.e]+)\)"/g)].map(
        ([, id, x, y]) => [id, { x: +x, y: +y }]
      )
    );
    /* One path per look of edge, plain then dangling then lit, each a run of segments in edge order. */
    const segments = [...html.matchAll(/<path class="edges (\w+)[^"]*" d="([^"]*)"/g)].flatMap(([, , d]) =>
      [...d.matchAll(/M[-\d.]+ [-\d.]+L([-\d.]+) ([-\d.]+)/g)].map(([, x, y]) => ({ x: +x, y: +y }))
    );
    const { nodes, edges } = graphElements(graph);
    const ordered = [...edges.filter((e) => !e.dangling), ...edges.filter((e) => e.dangling)];
    assert.equal(segments.length, 3, "one segment per edge, and a link made twice is one edge");
    ordered.forEach((edge, i) => {
      const target = at.get(edge.target);
      const radius = nodes.find((n) => n.id === edge.target).radius;
      const gap = Math.hypot(segments[i].x - target.x, segments[i].y - target.y);
      assert.ok(Math.abs(gap - (radius + 3)) < 0.2, `${edge.id} stops ${gap} short of a ${radius}px target`);
    });
  });

  it("takes a re-walk with the same concepts and links as the same layout", () => {
    const rewalked = structuredClone(graph);
    rewalked.concepts[0].stale = true;
    assert.equal(graphShape(graphElements(rewalked)), graphShape(graphElements(graph)), "new data, same shape");
    rewalked.links.push({ source: "big", target: "missing/other", dangling: true });
    assert.notEqual(graphShape(graphElements(rewalked)), graphShape(graphElements(graph)), "a new link is a new shape");
  });

  /* The CSS fades were guarded, but the force layout animated every tick regardless. */
  it("settles the layout off screen and draws it once under reduced motion", () => {
    const source = readFileSync(new URL("./OKFGraphView.svelte", import.meta.url), "utf8");
    assert.match(source, /import \{ prefersReducedMotion \} from "svelte\/motion";/);
    assert.match(source, /if \(prefersReducedMotion\.current\) \{\s*simulation\.stop\(\)\.tick\(\d+\);\s*scheduleDraw\(\);\s*\} else simulation\.restart\(\);/);
  });
});
