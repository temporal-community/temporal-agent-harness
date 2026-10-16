// ABOUTME: Pins the schema-driven form against the shapes that broke it under stress: a boolean
// that also grew a text box (and sent `true` for anything typed), a list of objects rendered as
// "[object Object]" and sent blank, 40 "X is required." strings run into one paragraph with no
// field marked, and an optional field that lost the title pydantic puts on its outer `anyOf`.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import SchemaForm from "./SchemaForm.svelte";
import {
  buildPayload,
  describeSchema,
  emptyValues,
  problemSummary,
  resultForm,
  validate
} from "./schemaForm.ts";

const travellers = resultForm({
  type: "array",
  title: "Travellers",
  items: {
    type: "object",
    properties: { name: { type: "string" }, email: { type: "string" } },
    required: ["name"]
  }
}).fields;

describe("schema form fields", () => {
  it("keeps an optional field's title, description and default from its outer anyOf", () => {
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
    assert.equal(choice.kind, "enum");
    assert.equal(choice.title, "Optional choice");
    assert.equal(choice.description, "Pick one if you like");
    assert.equal(choice.default, "b");
  });

  it("falls back to the property name for a blank title", () => {
    const [field] = describeSchema({ type: "object", properties: { city: { type: "string", title: "  " } } });
    assert.equal(field.title, "city");
  });

  it("renders a boolean as one checkbox, not a checkbox and a text box sharing its id", () => {
    const fields = describeSchema({ type: "object", properties: { ok: { type: "boolean" } } });
    const { body } = render(SchemaForm, { props: { fields, values: emptyValues(fields), idPrefix: "t" } });
    assert.equal(body.match(/<input/g)?.length, 1);
    assert.equal(body.match(/id="t-ok"/g)?.length, 1);
    assert.match(body, /type="checkbox"/);
  });

  it("sends a boolean only as a real true", () => {
    const fields = describeSchema({ type: "object", properties: { ok: { type: "boolean" } } });
    assert.deepEqual(buildPayload(fields, { ok: "false" }), { ok: false });
    assert.deepEqual(buildPayload(fields, { ok: true }), { ok: true });
  });

  it("renders each item of a list of objects as its own fields", () => {
    const values = { result: [{ name: "Ada", email: "" }] };
    const { body } = render(SchemaForm, { props: { fields: travellers, values, idPrefix: "t" } });
    assert.doesNotMatch(body, /\[object Object\]/);
    assert.match(body, /id="t-result-0-name"/);
    assert.match(body, /id="t-result-0-email"/);
    assert.match(body, /value="Ada"/);
  });

  it("checks each object in a list, and sends each as an object", () => {
    const values = { result: [{ name: "", email: "" }, { name: "Ada", email: "" }] };
    assert.deepEqual(validate(travellers, values), { "result.0.name": "Required." });
    values.result[0].name = "Grace";
    assert.deepEqual(validate(travellers, values), {});
    assert.deepEqual(buildPayload(travellers, values), {
      result: [{ name: "Grace" }, { name: "Ada" }]
    });
  });
});

describe("schema form validation", () => {
  const fields = describeSchema({
    type: "object",
    properties: {
      title: { type: "string" },
      count: { type: "integer", minimum: 1 },
      address: { type: "object", properties: { city: { type: "string" } }, required: ["city"] }
    },
    required: ["title", "count", "address"]
  });

  it("keys each problem to its field's path, nested fields included", () => {
    const problems = validate(fields, { title: "", count: "0", address: { city: "" } });
    assert.deepEqual(problems, {
      title: "Required.",
      count: "Must be at least 1.",
      "address.city": "Required."
    });
    assert.equal(problemSummary(problems), "Fix the 3 marked fields.");
    assert.equal(problemSummary({ title: "Required." }), "Fix the marked field.");
    assert.equal(problemSummary({}), null);
  });

  it("marks each invalid field beside its control", () => {
    const errors = validate(fields, { title: "", count: "2", address: { city: "" } });
    const { body } = render(SchemaForm, {
      props: { fields, values: { title: "", count: "2", address: { city: "" } }, idPrefix: "t", errors }
    });
    const titleInput = body.match(/<input[^>]*id="t-title"[^>]*>/)?.[0] ?? "";
    assert.match(titleInput, /aria-invalid="true"/);
    assert.match(titleInput, /aria-describedby="t-title-error"/);
    assert.match(body, /<p class="field-error[^"]*" id="t-title-error">Required\.<\/p>/);
    assert.match(body, /<p class="field-error[^"]*" id="t-address-city-error">Required\.<\/p>/);
    assert.doesNotMatch(body, /id="t-count-error"/);
  });
});
