// ABOUTME: Asserts a rearrangement of the graph travels rather than cutting, and that only the
// nodes entitled to travel do. A node present before and after moves between its two positions;
// a node that only just arrived is drawn where it belongs rather than sliding in from a place it
// never occupied. Selection is the third claim: the flow writes it onto the bound array and the
// projection does not know about it, so an interpolated array has to carry it or every frame of
// a live run drops the outline the reader clicked.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { lerpNodes, nodesMovedInPlace } from "./nodeTween.ts";

const previous = [
  { id: "a", position: { x: 0, y: 0 }, selected: true },
  { id: "b", position: { x: 100, y: 0 } }
];
const next = [
  { id: "a", position: { x: 100, y: 40 } },
  { id: "c", position: { x: 300, y: 0 } }
];

describe("a graph part-way between two projections", () => {
  it("puts a node that survived at the midpoint", () => {
    const a = lerpNodes(previous, next, 0.5).find((node) => node.id === "a");
    assert.deepEqual(a?.position, { x: 50, y: 20 });
  });

  it("carries selection across, because the projection cannot", () => {
    assert.equal(lerpNodes(previous, next, 0.5).find((node) => node.id === "a")?.selected, true);
    assert.equal(lerpNodes(previous, next, 1).find((node) => node.id === "a")?.selected, true);
  });

  it("puts a newly arrived node where it belongs, not in flight from nowhere", () => {
    assert.equal(lerpNodes(previous, next, 0.5).find((node) => node.id === "c")?.position.x, 300);
  });

  it("does not leave a departed node hanging mid-travel", () => {
    assert.ok(!lerpNodes(previous, next, 0.5).some((node) => node.id === "b"));
  });

  it("lands exactly where the projection put everything", () => {
    assert.deepEqual(
      lerpNodes(previous, next, 1).map((node) => node.position),
      next.map((node) => node.position)
    );
  });
});

describe("whether a rearrangement happened at all", () => {
  it("says no on a first paint, which has nowhere to travel from", () => {
    assert.equal(nodesMovedInPlace([], next), false);
  });

  it("says no when a wholly different graph replaced the old one", () => {
    assert.equal(nodesMovedInPlace(previous, [{ id: "z", position: { x: 5, y: 5 } }]), false);
  });

  it("says no when the node that stayed stayed put", () => {
    assert.equal(nodesMovedInPlace(previous, [{ id: "a", position: { x: 0, y: 0 } }]), false);
  });

  it("says yes when a node kept its id and changed place", () => {
    assert.equal(nodesMovedInPlace(previous, next), true);
  });
});
