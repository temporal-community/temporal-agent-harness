// ABOUTME: Asserts a rearrangement of the graph travels rather than cutting, and that only the
// nodes entitled to travel do. A node present before and after moves between its two positions;
// a node that only just arrived is drawn where it belongs rather than sliding in from a place it
// never occupied. Selection is the third claim: the flow writes it onto the bound array and the
// projection does not know about it, so an interpolated array has to carry it or every frame of
// a live run drops the outline the reader clicked. Between rearrangements, a node the rebuild left
// as it was is handed back as the same object, so xyflow neither re-measures nor re-renders it.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { keepUnchanged, lerpNodes, nodesMovedInPlace } from "./nodeTween.ts";

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

/* A rebuilt node as the projection makes it, and as the flow leaves it on the bound array. */
const card = (id, text, extra = {}) => ({
  id,
  type: "agentState",
  position: { x: 0, y: 0 },
  width: 230,
  height: 130,
  data: { title: id, detail: text, metrics: [{ label: "in", value: "1" }], context: [{ label: "Node", text: id }] },
  ...extra
});

describe("handing the flow back what did not change", () => {
  const onCanvas = [
    card("a", "hello", { measured: { width: 230, height: 130 }, selected: true }),
    card("b", "streaming", { measured: { width: 230, height: 130 } })
  ];

  it("keeps the object for a node the rebuild did not change, and the array when none changed", () => {
    const kept = keepUnchanged(onCanvas, [card("a", "hello"), card("b", "streaming")]);
    assert.equal(kept, onCanvas);
  });

  it("swaps only the node that changed, keeping what the flow wrote on it", () => {
    const kept = keepUnchanged(onCanvas, [card("a", "hello"), card("b", "streaming more")]);
    assert.notEqual(kept, onCanvas);
    assert.equal(kept[0], onCanvas[0]);
    assert.notEqual(kept[1], onCanvas[1]);
    assert.equal(kept[1].data.detail, "streaming more");
    assert.deepEqual(kept[1].measured, { width: 230, height: 130 });
  });

  it("compares nested data by value, not by reference", () => {
    const changed = card("a", "hello");
    changed.data.metrics = [{ label: "in", value: "2" }];
    const kept = keepUnchanged(onCanvas, [changed, card("b", "streaming")]);
    assert.notEqual(kept[0], onCanvas[0]);
    assert.equal(kept[0].data.metrics[0].value, "2");
    assert.equal(kept[0].selected, true);
  });

  it("keeps the data object of a node that only moved, and drops a stale measurement on resize", () => {
    const moved = keepUnchanged(onCanvas, [card("a", "hello", { position: { x: 5, y: 0 } }), card("b", "streaming", { height: 231 })]);
    assert.equal(moved[0].data, onCanvas[0].data);
    assert.equal(moved[0].position.x, 5);
    assert.equal(moved[1].measured, undefined);
  });

  it("notices an added, removed, or reordered node", () => {
    assert.notEqual(keepUnchanged(onCanvas, [card("a", "hello")]), onCanvas);
    const reordered = keepUnchanged(onCanvas, [card("b", "streaming"), card("a", "hello")]);
    assert.notEqual(reordered, onCanvas);
    assert.equal(reordered[0], onCanvas[1]);
  });
});
