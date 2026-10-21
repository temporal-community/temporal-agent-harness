// Python's tests/state/test_property.py: applying every patch in version order to the version-0
// snapshot yields the committed value, and a commit that fails publishes and changes nothing.

import assert from "node:assert/strict";
import { test } from "node:test";
import { HarnessStateError } from "@temporalio/agent-harness";
import fc from "fast-check";
import { harness, replay, type Plan } from "./state-support.ts";

interface Edit {
  recipe: number;
  n: number;
  key: string;
  value: string;
}

const recipes: ((d: Plan, e: Edit) => void)[] = [
  (d, e) => void (d.goal = e.value),
  (d, e) => void d.steps.push({ name: e.value, done: false }),
  (d, e) => void d.steps.splice(e.n % (d.steps.length + 1), 0, { name: e.value, done: true }),
  (d, e) => void d.steps.splice(e.n % (d.steps.length + 1), 1),
  (d, e) => d.steps.length && void (d.steps[e.n % d.steps.length]!.done = !d.steps[0]!.done),
  (d) => void d.steps.reverse(),
  (d) => void d.steps.sort((x, y) => x.name.localeCompare(y.name)),
  (d, e) => void (d.scratch[e.key] = e.value),
  (d, e) => void delete d.scratch[e.key],
  (d) => void (d.steps = d.steps.filter((s) => s.done)),
];

const text = fc.string({ unit: "grapheme", maxLength: 8 });
// JSON Pointer's escapes, array-index and append tokens, an inherited member's name, and astral and
// RTL text, besides random keys.
const key = fc.oneof(fc.constantFrom("a", "-", "", "a/b", "~0", "0", "toString", "דּ", "👩‍💻"), text);
const plan = fc.record({
  goal: text,
  steps: fc.array(fc.record({ name: text, done: fc.boolean() }), { maxLength: 4 }),
  scratch: fc.dictionary(key, text, { maxKeys: 4, noNullPrototype: true }),
});
const edit = fc.record({ recipe: fc.nat(recipes.length - 1), n: fc.nat(), key, value: text });
const edits = fc.array(edit, { maxLength: 6 });
const commit = fc.oneof(
  { arbitrary: fc.record({ set: plan }), weight: 1 },
  { arbitrary: fc.record({ edits, fails: fc.constant(false) }), weight: 8 },
  { arbitrary: fc.record({ edits, fails: fc.constant(true) }), weight: 1 },
);

test("random commits, failed ones included, always replay to the committed value", () => {
  fc.assert(
    fc.property(plan, fc.array(commit, { minLength: 1, maxLength: 20 }), (initial, commits) => {
      const { ref, published } = harness(() => initial);
      const start = ref.snapshot().value;
      for (const c of commits) {
        const before = ref.current;
        try {
          if ("set" in c) ref.set(c.set);
          else
            ref.mutate((d) => {
              for (const e of c.edits) recipes[e.recipe]!(d, e);
              if (c.fails) (d.steps as unknown[]).push({ name: 1 });
            });
        } catch (e) {
          assert.ok(e instanceof HarnessStateError);
          assert.equal(ref.current, before);
        }
        assert.equal(ref.version, published.length);
        assert.deepStrictEqual(
          replay(start, published as { ops: Record<string, unknown>[] }[]),
          ref.current,
        );
      }
      assert.deepStrictEqual(
        published.map((e) => e.version),
        published.map((_, i) => i + 1),
      );
    }),
    // A fixed seed keeps CI runs identical; drop it locally to explore new cases.
    { seed: 0x5eed, numRuns: 200 },
  );
});
