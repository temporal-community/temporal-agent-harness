// applyOps: the RFC 6902 applier a session's observable state runs on.

import assert from "node:assert/strict";
import { test } from "node:test";

import type { JsonPatchOp, JsonValue } from "../src/frames.ts";
import { applyOps, buildPointer, escapeToken, parsePointer, valueAt } from "../src/jsonPatch.ts";

interface Doc {
  goal: string;
  steps: Array<{ name: string; done: boolean }>;
  scratch: Record<string, string>;
  tags: string[];
}

const doc = (): Doc & JsonValue => ({
  goal: "ship it",
  steps: [{ name: "research", done: false }],
  scratch: { cwd: "/repo" },
  tags: ["docs"]
});

function apply(ops: JsonPatchOp[], start: JsonValue = doc()) {
  const result = applyOps(start, ops);
  return { ...result, doc: result.doc as unknown as Doc };
}

test("pointers escape and unescape in the order the RFC requires", () => {
  /* The other order turns an escaped `~1` back into a slash, so a key of
     literally "~1" and a key of "/" would both parse to "/". */
  assert.equal(escapeToken("a/b~c"), "a~1b~0c");
  assert.deepEqual(parsePointer("/a~1b~0c"), ["a/b~c"]);
  assert.deepEqual(parsePointer("/~01"), ["~1"]);
  assert.equal(buildPointer(["a/b", 0]), "/a~1b/0");
});

test("the empty pointer is the whole document, not one empty token", () => {
  assert.deepEqual(parsePointer(""), []);
  assert.deepEqual(parsePointer("/"), [""]);
  assert.equal(buildPointer([]), "");
  assert.deepEqual(valueAt(doc(), ""), doc());
});

test("pointers resolve into arrays and objects alike", () => {
  assert.equal(valueAt(doc(), "/steps/0/name"), "research");
  assert.equal(valueAt(doc(), "/scratch/cwd"), "/repo");
  assert.equal(valueAt(doc(), "/steps/9/name"), undefined);
  assert.equal(valueAt(doc(), "/goal/deeper"), undefined);
});

test("an append reports the index it landed at", () => {
  const { doc: next, applied, error } = apply([
    { op: "add", path: "/steps/-", value: { name: "write", done: false } }
  ]);
  assert.equal(error, null);
  /* `/steps/-` is not addressable; `/steps/1` is, and a view marks rows by it. */
  assert.equal(applied[0]!.path, "/steps/-");
  assert.equal(applied[0]!.resolved, "/steps/1");
  assert.equal(applied[0]!.before, undefined);
  assert.equal(next.steps[1]!.name, "write");
});

test("an add at an array index inserts rather than overwrites", () => {
  const { doc: next, error } = apply([{ op: "add", path: "/tags/0", value: "first" }]);
  assert.equal(error, null);
  assert.deepEqual(next.tags, ["first", "docs"]);
});

test("a replace carries what stood at the path before", () => {
  const { doc: next, applied } = apply([
    { op: "replace", path: "/steps/0/done", value: true },
    { op: "replace", path: "/tags/0", value: "api" }
  ]);
  assert.equal(applied[0]!.before, false);
  assert.equal(applied[0]!.value, true);
  assert.equal(applied[1]!.before, "docs");
  assert.equal(applied[1]!.resolved, "/tags/0");
  assert.deepEqual(next.tags, ["api"]);
});

test("the empty path replaces the whole document", () => {
  /* `ref.set()` emits exactly this, and nothing else does. */
  const { doc: next, applied, error } = apply([
    { op: "replace", path: "", value: { goal: "start over" } }
  ]);
  assert.equal(error, null);
  assert.deepEqual(next, { goal: "start over" });
  assert.equal(applied[0]!.resolved, "");
  assert.deepEqual(applied[0]!.before, doc());
});

test("remove works on an array by index and on an object by key, and carries no value", () => {
  const { doc: next, applied, error } = apply([
    { op: "remove", path: "/steps/0" },
    { op: "remove", path: "/scratch/cwd" }
  ]);
  assert.equal(error, null);
  assert.deepEqual(next.steps, []);
  assert.deepEqual(next.scratch, {});
  assert.deepEqual(applied[0]!.before, { name: "research", done: false });
  assert.equal(applied[1]!.before, "/repo");
  assert.ok(!("value" in applied[0]!) && !("value" in applied[1]!));
});

test("an inserted value is copied away from the op that carried it", () => {
  /* The failure this prevents: `add /steps/-` puts the op's own object into the
     document, then `replace /steps/1/done` writes through it and mutates the
     FRAME, and every rebuild from the frame log starts from a document the agent
     was never in. */
  const value = { name: "write", done: false };
  const { doc: next } = apply([
    { op: "add", path: "/steps/-", value },
    { op: "replace", path: "/steps/1/done", value: true }
  ]);
  assert.equal(next.steps[1]!.done, true);
  assert.equal(value.done, false, "the op was written through");
});

test("a value held behind a Proxy, as a reactive store holds frames, still applies", () => {
  const value = new Proxy({ name: "write", done: false }, {}) as unknown as JsonValue;
  const { doc: next, error } = apply([{ op: "add", path: "/steps/-", value }]);
  assert.equal(error, null);
  assert.deepEqual(next.steps[1], { name: "write", done: false });
});

test("the first op that cannot apply stops the patch, and says which", () => {
  const { doc: next, applied, error } = apply([
    { op: "replace", path: "/goal", value: "landed" },
    { op: "replace", path: "/steps/7", value: { name: "nowhere", done: false } },
    { op: "replace", path: "/goal", value: "never reached" }
  ]);
  /* A patch is an ordered whole: applying the tail of one whose head failed
     builds a document that never existed. */
  assert.equal(applied.length, 1);
  assert.equal(next.goal, "landed");
  assert.match(error!, /outside an array of 1/);
});

test("array bounds: add may land one past the end, replace and remove may not", () => {
  assert.equal(apply([{ op: "add", path: "/tags/1", value: "x" }]).error, null);
  assert.match(apply([{ op: "add", path: "/tags/2", value: "x" }]).error!, /outside an array of 1/);
  assert.match(apply([{ op: "replace", path: "/tags/1", value: "x" }]).error!, /outside an array/);
  assert.match(apply([{ op: "remove", path: "/tags/1" }]).error!, /outside an array/);
  assert.match(apply([{ op: "remove", path: "/tags/-" }]).error!, /outside an array/);
  assert.match(apply([{ op: "add", path: "/tags/one", value: "x" }]).error!, /outside an array/);
});

test("an op kind the harness does not emit is refused", () => {
  const move = { op: "move", from: "/steps/0", path: "/steps/1" } as unknown as JsonPatchOp;
  /* Not a gap to fill. A `move` on this stream means the two ends disagree
     about the contract, and quietly implementing it would hide that. */
  assert.match(apply([move]).error!, /unsupported op "move" at \/steps\/1/);
});

test("an op with no path is refused rather than thrown on", () => {
  const noPath = { op: "add", value: 1 } as unknown as JsonPatchOp;
  assert.equal(apply([noPath]).error, "a patch op arrived with no path");
});

test("a missing container is named rather than thrown on", () => {
  assert.match(
    apply([{ op: "add", path: "/nowhere/deep/key", value: 1 }]).error!,
    /no container at \/nowhere\/deep for add/
  );
  assert.match(apply([{ op: "add", path: "/goal/key", value: 1 }]).error!, /no container at \/goal/);
});

test("a pointer through __proto__ or constructor cannot reach Object.prototype", () => {
  for (const path of ["/__proto__/polluted", "/constructor/prototype/polluted"]) {
    const result = applyOps({}, [{ op: "add", path, value: true }]);
    assert.match(result.error!, /no container/);
    assert.equal(({} as Record<string, unknown>)["polluted"], undefined);
  }
});

test("__proto__ as the final key is refused rather than re-parenting the object", () => {
  const target: JsonValue = {};
  const result = applyOps(target, [{ op: "add", path: "/__proto__", value: { polluted: true } }]);
  assert.match(result.error!, /names __proto__/);
  assert.equal(Object.getPrototypeOf(target), Object.prototype);
});

test("inherited keys are not in the document", () => {
  assert.equal(valueAt({}, "/constructor"), undefined);
  assert.match(applyOps({}, [{ op: "remove", path: "/toString" }]).error!, /no such key/);
});
