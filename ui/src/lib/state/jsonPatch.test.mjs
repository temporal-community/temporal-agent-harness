// ABOUTME: Pins the RFC 6902 applier the state pane runs on. Three of these are the reasons this
// file exists rather than a dependency: `/xs/-` has to come back as a concrete index or a panel has
// nothing to mark, an inserted value has to be copied away from the frame that carried it or a
// later op writes through into the console's own event record, and an op that cannot land has to
// come back as a sentence rather than an exception — a pane that throws stops showing the run.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { applyOps, buildPointer, escapeToken, parsePointer, valueAt } from "./jsonPatch.ts";

const doc = () => ({
  goal: "ship it",
  steps: [{ name: "research", done: false }],
  scratch: { cwd: "/repo" },
  tags: ["docs"]
});

describe("RFC 6901 pointers", () => {
  it("escapes and unescapes in the order the RFC requires", () => {
    /* The other order turns an escaped `~1` back into a slash, so a key of
       literally "~1" and a key of "/" would both parse to "/". */
    assert.equal(escapeToken("a/b~c"), "a~1b~0c");
    assert.deepEqual(parsePointer("/a~1b~0c"), ["a/b~c"]);
    assert.deepEqual(parsePointer("/~01"), ["~1"]);
  });

  it("reads the empty pointer as the whole document, not as one empty token", () => {
    assert.deepEqual(parsePointer(""), []);
    assert.equal(buildPointer([]), "");
    assert.deepEqual(valueAt(doc(), ""), doc());
  });

  it("resolves into arrays and objects alike", () => {
    assert.equal(valueAt(doc(), "/steps/0/name"), "research");
    assert.equal(valueAt(doc(), "/scratch/cwd"), "/repo");
    assert.equal(valueAt(doc(), "/steps/9/name"), undefined);
  });
});

describe("applying ops", () => {
  it("reports where an append landed", () => {
    const { applied, error } = applyOps(doc(), [
      { op: "add", path: "/steps/-", value: { name: "write", done: false } }
    ]);
    assert.equal(error, null);
    /* `/steps/-` is not addressable; `/steps/1` is, which is the whole point. */
    assert.equal(applied[0].path, "/steps/-");
    assert.equal(applied[0].resolved, "/steps/1");
  });

  it("carries what stood at a path before it was replaced", () => {
    const { applied } = applyOps(doc(), [
      { op: "replace", path: "/steps/0/done", value: true }
    ]);
    assert.equal(applied[0].before, false);
    assert.equal(applied[0].value, true);
  });

  it("replaces the whole document at the empty path", () => {
    /* `ref.set()` emits exactly this, and nothing else does. */
    const { doc: next, applied, error } = applyOps(doc(), [
      { op: "replace", path: "", value: { goal: "start over" } }
    ]);
    assert.equal(error, null);
    assert.deepEqual(next, { goal: "start over" });
    assert.equal(applied[0].resolved, "");
  });

  it("removes from an array by index and from an object by key", () => {
    const { doc: next, error } = applyOps(doc(), [
      { op: "remove", path: "/steps/0" },
      { op: "remove", path: "/scratch/cwd" }
    ]);
    assert.equal(error, null);
    assert.deepEqual(next.steps, []);
    assert.deepEqual(next.scratch, {});
  });

  it("copies an inserted value away from the op that carried it", () => {
    /* The failure this prevents: `add /steps/-` puts the op's own object into the
       document, then `replace /steps/1/done` writes through it and mutates the
       FRAME. Every projection rebuilds from those frames, so the next rebuild
       would start from a document the agent was never in. */
    const ops = [
      { op: "add", path: "/steps/-", value: { name: "write", done: false } },
      { op: "replace", path: "/steps/1/done", value: true }
    ];
    const { doc: next } = applyOps(doc(), ops);
    assert.equal(next.steps[1].done, true);
    assert.equal(ops[0].value.done, false, "the op list was written through");
  });

  it("stops at the first op it cannot apply, and says which", () => {
    const { doc: next, applied, error } = applyOps(doc(), [
      { op: "replace", path: "/goal", value: "landed" },
      { op: "replace", path: "/steps/7", value: { name: "nowhere" } },
      { op: "replace", path: "/goal", value: "never reached" }
    ]);
    /* A patch is an ordered whole: applying the tail of one whose head failed
       builds a document that never existed. */
    assert.equal(applied.length, 1);
    assert.equal(next.goal, "landed");
    assert.match(error, /outside an array of 1/);
  });

  it("refuses an op kind the harness does not emit", () => {
    const { error } = applyOps(doc(), [
      { op: "move", from: "/steps/0", path: "/steps/1" }
    ]);
    /* Not a gap to fill. A `move` on this stream means the two ends disagree
       about the contract, and quietly implementing it would hide that. */
    assert.match(error, /unsupported op/);
  });

  it("names a missing container rather than throwing", () => {
    const { error } = applyOps(doc(), [
      { op: "add", path: "/nowhere/deep/key", value: 1 }
    ]);
    assert.match(error, /no container at/);
  });
});
