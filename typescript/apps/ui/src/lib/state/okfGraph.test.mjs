// ABOUTME: Tests for the OKF graph helpers: type colors in order of first appearance, ghost nodes
// for dangling links, backlinks, search, stripping frontmatter from a concept's text, and
// resolving a link the way the bundle walk does.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  graphElements,
  linkTarget,
  linksOf,
  matches,
  seededRandom,
  stripFrontmatter,
  typeColors
} from "./okfGraph.ts";

const concept = (id, type, extra = {}) => ({
  id,
  path: `${id}.md`,
  type,
  title: id.split("/").pop(),
  description: "",
  tags: [],
  status: "stable",
  trust_tier: "unverified",
  stale: false,
  size: 100,
  problem: null,
  ...extra
});

const graph = {
  concepts: [
    concept("people/ana", "Person", { tags: ["family"] }),
    concept("trip", "Plan"),
    concept("odd", "Unknown", { problem: "no frontmatter" }),
    concept("bea", "Person")
  ],
  links: [
    { source: "trip", target: "people/ana", dangling: false },
    { source: "trip", target: "flights", dangling: true },
    { source: "bea", target: "people/ana", dangling: false }
  ],
  truncated: false,
  walked_at: 0
};

describe("okfGraph", () => {
  it("colors types in order of first appearance, and Unknown grey", () => {
    const colors = typeColors(graph.concepts);
    assert.deepEqual([...colors.entries()], [
      ["Person", "--accent"],
      ["Plan", "--reasoning"],
      ["Unknown", "--text-4"]
    ]);
    for (const token of colors.values()) assert.notEqual(token, "--tool");
  });

  it("adds a ghost node for each link to a concept that doesn't exist", () => {
    const { nodes, edges } = graphElements(graph);
    assert.deepEqual(
      nodes.map((n) => [n.id, n.ghost]),
      [
        ["people/ana", false],
        ["trip", false],
        ["odd", false],
        ["bea", false],
        ["flights", true]
      ]
    );
    assert.deepEqual(
      edges.map((e) => [e.source, e.target, e.dangling]),
      [
        ["trip", "people/ana", false],
        ["trip", "flights", true],
        ["bea", "people/ana", false]
      ]
    );
  });

  it("finds a concept's backlinks and links", () => {
    const { citedBy, linksTo } = linksOf(graph, "people/ana");
    assert.deepEqual(citedBy.map((l) => l.source), ["trip", "bea"]);
    assert.deepEqual(linksTo, []);
  });

  it("searches titles, ids and tags", () => {
    const [ana, trip] = graphElements(graph).nodes;
    assert.ok(matches(ana, "FAMILY"));
    assert.ok(matches(ana, "people/"));
    assert.ok(!matches(trip, "family"));
    assert.ok(matches(trip, "  "));
  });

  it("strips frontmatter from a concept's text", () => {
    assert.equal(stripFrontmatter("---\ntype: Plan\n---\n\nFly on the 14th.\n"), "Fly on the 14th.\n");
    assert.equal(stripFrontmatter("No frontmatter."), "No frontmatter.");
    assert.equal(stripFrontmatter("---\nnever closed\n"), "---\nnever closed\n");
  });

  it("resolves links the way the bundle walk does", () => {
    assert.equal(linkTarget("trips/denver.md", "../people/ana.md#bio"), "people/ana");
    assert.equal(linkTarget("trips/denver.md", "/people/ana.md"), "people/ana");
    assert.equal(linkTarget("trips/denver.md", "flights.md"), "trips/flights");
    assert.equal(linkTarget("trips/denver.md", "my%20notes.md"), "trips/my notes");
    assert.equal(linkTarget("a.md", "https://e.com/x.md"), null);
    assert.equal(linkTarget("a.md", "../../outside.md"), null);
    assert.equal(linkTarget("a.md", "index.md"), null);
    assert.equal(linkTarget("a.md", "picture.png"), null);
  });

  it("lays out from a seeded random source", () => {
    const a = seededRandom(3);
    const b = seededRandom(3);
    const first = [a(), a(), a()];
    assert.deepEqual(first, [b(), b(), b()]);
    for (const value of first) assert.ok(value >= 0 && value < 1);
  });
});
