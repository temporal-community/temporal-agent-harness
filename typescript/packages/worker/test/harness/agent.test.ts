// tool_end's tool_output (core's harness/agent.ts): a tool's result as JSON, failing the call
// wherever JSON.stringify would change the value silently.

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { ApplicationFailure } from "@temporalio/common";
import { toolOutputJson } from "@temporalio/agent-harness";

function failure(result: unknown): ApplicationFailure {
  try {
    toolOutputJson("t", result);
  } catch (e) {
    assert.ok(e instanceof ApplicationFailure);
    return e;
  }
  assert.fail("expected toolOutputJson to throw");
}

describe("toolOutputJson", () => {
  it("passes JSON through and keeps a string a string", () => {
    assert.equal(toolOutputJson("t", "hi"), "hi");
    assert.equal(toolOutputJson("t", 5), 5);
    assert.equal(toolOutputJson("t", false), false);
    assert.deepStrictEqual(toolOutputJson("t", { a: [1, null, { b: "c" }] }), {
      a: [1, null, { b: "c" }],
    });
  });

  it("gives null for no result and leaves undefined properties out", () => {
    assert.equal(toolOutputJson("t", undefined), null);
    assert.deepStrictEqual(toolOutputJson("t", { a: 1, b: undefined }), { a: 1 });
  });

  it("encodes through toJSON and reads a class instance's own fields", () => {
    class Receipt {
      readonly id: string;
      readonly at: Date;
      constructor(id: string, at: Date) {
        this.id = id;
        this.at = at;
      }
    }
    assert.deepStrictEqual(toolOutputJson("t", new Receipt("r1", new Date(0))), {
      id: "r1",
      at: "1970-01-01T00:00:00.000Z",
    });
  });

  const cyclic: { self?: unknown } = {};
  cyclic.self = cyclic;
  const cases: [string, unknown, string][] = [
    ["a Map", new Map([["k", 1]]), "at $: a Map"],
    ["a Set inside an object", { tags: new Set(["x"]) }, "at $.tags: a Set"],
    ["NaN", { ratio: Number.NaN }, "at $.ratio: NaN"],
    ["Infinity", [Number.POSITIVE_INFINITY], "at $[0]: Infinity"],
    ["a bigint", { n: 10n }, "at $.n: a bigint"],
    ["a function property", { f: () => 1 }, "at $.f: a function"],
    ["a symbol", Symbol("s"), "at $: a symbol"],
    ["an undefined array item", [1, undefined], "at $[1]: undefined"],
    ["a RegExp", /x/, "at $: a RegExp"],
    ["a typed array", new Uint8Array([1]), "at $: a Uint8Array"],
    ["a cycle", cyclic, "at $.self: a cycle"],
  ];
  for (const [name, value, where] of cases)
    it(`fails the call for ${name}`, () => {
      const e = failure(value);
      assert.equal(e.type, "ToolOutputNotJson");
      assert.equal(e.nonRetryable, true);
      assert.equal(e.message, `tool 't' returned a value with no JSON form ${where}`);
    });
});
