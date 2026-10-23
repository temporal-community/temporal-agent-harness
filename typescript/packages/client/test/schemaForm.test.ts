// The schema form's pure half against the shapes that broke it under stress: a boolean that sent
// `true` for anything typed, a list of objects sent blank, problems not keyed to their field, and
// an optional field that lost the title pydantic puts on its outer `anyOf`.

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { buildPayload, describeSchema, resultForm, validate } from "../src/schemaForm.ts";

const travelers = resultForm({
  type: "array",
  title: "Travelers",
  items: {
    type: "object",
    properties: { name: { type: "string" }, email: { type: "string" } },
    required: ["name"]
  }
}).fields;

describe("schema form fields", () => {
  test("keeps an optional field's title, description and default from its outer anyOf", () => {
    const [choice] = describeSchema({
      type: "object",
      properties: {
        choice: {
          anyOf: [{ type: "string", enum: ["a", "b"] }, { type: "null" }],
          title: "Optional choice",
          description: "Pick one if you like",
          default: "b"
        }
      }
    });
    assert.equal(choice?.kind, "enum");
    assert.equal(choice?.title, "Optional choice");
    assert.equal(choice?.description, "Pick one if you like");
    assert.equal(choice?.default, "b");
  });

  test("falls back to the property name for a blank title", () => {
    const [field] = describeSchema({ type: "object", properties: { city: { type: "string", title: "  " } } });
    assert.equal(field?.title, "city");
  });

  test("sends a boolean only as a real true", () => {
    const fields = describeSchema({ type: "object", properties: { ok: { type: "boolean" } } });
    assert.deepEqual(buildPayload(fields, { ok: "false" }), { ok: false });
    assert.deepEqual(buildPayload(fields, { ok: true }), { ok: true });
  });

  test("checks each object in a list, and sends each as an object", () => {
    const first = { name: "", email: "" };
    const values = { result: [first, { name: "Ada", email: "" }] };
    assert.deepEqual(validate(travelers, values), { "result.0.name": "Required." });
    first.name = "Grace";
    assert.deepEqual(validate(travelers, values), {});
    assert.deepEqual(buildPayload(travelers, values), {
      result: [{ name: "Grace" }, { name: "Ada" }]
    });
  });
});

test("keys each problem to its field's path, nested fields included", () => {
  const fields = describeSchema({
    type: "object",
    properties: {
      title: { type: "string" },
      count: { type: "integer", minimum: 1 },
      address: { type: "object", properties: { city: { type: "string" } }, required: ["city"] }
    },
    required: ["title", "count", "address"]
  });
  assert.deepEqual(validate(fields, { title: "", count: "0", address: { city: "" } }), {
    title: "Required.",
    count: "Must be at least 1.",
    "address.city": "Required."
  });
});

/* Length, pattern, exclusive bounds and list sizes were only caught by the server's 422, after
   the round trip and away from the field: tictactoe's player_name is 1-40 characters. */
test("checks schema constraints beside each field, counting characters and non-blank entries", () => {
  const fields = describeSchema({
    type: "object",
    properties: {
      player_name: { type: "string", minLength: 1, maxLength: 40 },
      code: { anyOf: [{ type: "string", pattern: "^[A-Z]{3}$" }, { type: "null" }] },
      odds: { type: "number", exclusiveMinimum: 0, exclusiveMaximum: 1 },
      tags: { type: "array", items: { type: "string" }, minItems: 1, maxItems: 2 }
    },
    required: ["player_name", "odds", "tags"]
  });
  const ok = { player_name: "Ada", code: "", odds: "0.5", tags: ["x"] };
  assert.deepEqual(validate(fields, ok), {}, "a blank optional is not checked");
  assert.deepEqual(validate(fields, { ...ok, player_name: "👩‍👩‍👧".repeat(5) + "x".repeat(15) }), {});
  assert.deepEqual(
    validate(fields, { player_name: "x".repeat(41), code: "abc", odds: "1", tags: ["", ""] }),
    {
      player_name: "Must be at most 40 characters.",
      code: "Must match the pattern ^[A-Z]{3}$.",
      odds: "Must be less than 1.",
      tags: "Add at least 1 entry."
    }
  );
  assert.equal(validate(fields, { ...ok, odds: "0" }).odds, "Must be more than 0.");
  assert.equal(validate(fields, { ...ok, tags: ["a", "b", "c"] }).tags, "Keep at most 2 entries.");
  const unparseable = describeSchema({ type: "object", properties: { s: { type: "string", pattern: "(?<" } } });
  assert.deepEqual(validate(unparseable, { s: "anything" }), {}, "left to the server");
});
