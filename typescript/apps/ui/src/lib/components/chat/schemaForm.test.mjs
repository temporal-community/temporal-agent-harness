// ABOUTME: Pins the schema-driven form against the shapes that broke it under stress: a boolean
// that also grew a text box, a list of objects rendered as "[object Object]", and 40 "X is
// required." strings run into one paragraph with no field marked. The describe/validate/payload
// half is pinned in the client package's test/schemaForm.test.ts.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { describeSchema, emptyValues, resultForm, validate } from "@temporalio/agent-harness-client";
import SchemaForm from "./SchemaForm.svelte";
import { focusFirstProblem, problemSummary } from "./schemaForm.ts";
import { stripComments } from "../../../../tests/support/controllerHarness.mjs";

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
  it("renders a boolean as one checkbox, not a checkbox and a text box sharing its id", () => {
    const fields = describeSchema({ type: "object", properties: { ok: { type: "boolean" } } });
    const { body } = render(SchemaForm, { props: { fields, values: emptyValues(fields), idPrefix: "t" } });
    assert.equal(body.match(/<input/g)?.length, 1);
    assert.equal(body.match(/id="t-ok"/g)?.length, 1);
    assert.match(body, /type="checkbox"/);
  });

  it("renders each item of a list of objects as its own fields", () => {
    const values = { result: [{ name: "Ada", email: "" }] };
    const { body } = render(SchemaForm, { props: { fields: travellers, values, idPrefix: "t" } });
    assert.doesNotMatch(body, /\[object Object\]/);
    assert.match(body, /id="t-result-0-name"/);
    assert.match(body, /id="t-result-0-email"/);
    assert.match(body, /value="Ada"/);
  });
});

describe("schema form in the gate card's design", () => {
  // Thirteen models: one past the dozen a row of buttons holds, so this one stays a dropdown.
  const models = Array.from({ length: 13 }, (_, i) => `m${i}`);
  const fields = describeSchema({
    type: "object",
    properties: {
      model: { title: "Model", enum: models },
      label: { type: "string" },
      size: { type: "integer" },
      notes: { type: "string", format: "textarea" },
      tags: { type: "array", items: { type: "string" } }
    },
    required: ["model"]
  });
  const html = (props) =>
    render(SchemaForm, { props: { fields, values: { model: "m0", label: "", size: "", notes: "", tags: ["x"] }, idPrefix: "t", ...props } }).body;

  it("sends with a submit Chip named for the handler, and draws no button of its own", () => {
    const body = html({ send: { handler: "set_model" } });
    const submits = body.match(/<button[^>]*type="submit"[^>]*>/g) ?? [];
    assert.equal(submits.length, 1);
    assert.match(submits[0], /class="chip [^"]*\bquiet\b[^"]*\bdense\b/);
    assert.match(submits[0], /aria-label="Send set_model"/);
    assert.match(body, /<div class="schema-foot[^"]*">/);
    for (const button of body.match(/<button[^>]*>/g) ?? []) assert.match(button, /class="chip /);
    assert.doesNotMatch(html(), /type="submit"/, "the submit row is opt-in");
  });

  it("puts the form-level alert above the Send chip", () => {
    const body = html({ send: { handler: "set_model", alert: "Fix the marked field." } });
    assert.match(body, /role="alert">Fix the marked field\.<\/p>[\s\S]*type="submit"/);
  });

  it("gives every input and textarea the shared control box, and a dropdown the shared Select", () => {
    const controls = html().match(/<(?:input|textarea)\b[^>]*>/g) ?? [];
    assert.equal(controls.length, 4);
    for (const control of controls) assert.match(control, /class="form-control\b/);
    assert.match(stripComments(html()), /<span class="field select[^"]*">\s*<select[^>]*id="t-model"/);
  });

  it("shows an unanswered required dropdown as unanswered, and says it is required", () => {
    const body = html({ values: { model: "", label: "", size: "", notes: "", tags: [] } });
    const select = body.match(/<select[^>]*id="t-model"[^>]*>[\s\S]*?<\/select>/)?.[0] ?? "";
    assert.match(select, /aria-required="true"/);
    assert.match(select, /<option value="" disabled[^>]*selected[^>]*>Choose…<\/option>/);
    assert.doesNotMatch(select, /value="m0"[^>]*selected/, "the first choice is not drawn as picked");
    assert.doesNotMatch(select, /\(unset\)/, "a required field offers no way to unset it");
  });

  it("marks a required field with a muted star and a read-out \"required\"", () => {
    const label = html().match(/<label[^>]*for="t-model"[^>]*>[\s\S]*?<\/label>/)?.[0] ?? "";
    assert.match(label, /class="kicker\b/);
    assert.match(label, /<span class="req[^"]*" aria-hidden="true">\*<\/span><span class="visually-hidden[^"]*">required<\/span>/);
    assert.doesNotMatch(html().match(/<label[^>]*for="t-label"[^>]*>[\s\S]*?<\/label>/)?.[0] ?? "", /required/);
  });
});

describe("schema form choices as a list", () => {
  const fields = describeSchema({
    type: "object",
    properties: {
      city: { title: "City", enum: ["Oregon", "Maine"] },
      extras: { title: "Extras", type: "array", items: { enum: ["Lounge", "Wifi"] } }
    },
    required: ["city"]
  });
  const html = (props) =>
    render(SchemaForm, { props: { fields, values: { city: "", extras: [] }, idPrefix: "t", choices: "list", ...props } }).body;

  it("asks one answer as radios and several as checkboxes, with nothing picked and no dropdown", () => {
    const body = html();
    assert.equal(body.match(/type="radio"/g)?.length, 2);
    assert.equal(body.match(/type="checkbox"/g)?.length, 2);
    assert.doesNotMatch(body, /<select|\bchecked\b/);
    assert.match(body, /<fieldset class="choices[^"]*"[^>]*>\s*<legend[^>]*>\s*City/);
    assert.doesNotMatch(body, /Something else…/, "the free-text row is opt-in");
  });

  it("offers \"Something else…\" when bound, its text box only once picked", () => {
    assert.equal(html({ other: {} }).match(/data-other=""/g)?.length, 2);
    assert.doesNotMatch(html({ other: {} }), /Say it in your own words/);
    const picked = html({ other: { city: "Portland" } });
    assert.match(picked, /<input[^>]*type="radio"[^>]*data-other=""[^>]*checked/);
    assert.match(picked, /placeholder="Say it in your own words"/);
  });

  it("draws a short set as buttons by default, not radios", () => {
    const body = render(SchemaForm, { props: { fields, values: { city: "", extras: [] }, idPrefix: "t" } }).body;
    assert.match(body, /role="radiogroup"/);
    assert.doesNotMatch(body, /type="radio"/);
  });
});

// Tic-tac-toe's cell is an integer from 1 to 9; it was a number box to type a digit into.
describe("schema form small value sets as buttons", () => {
  const fields = describeSchema({
    type: "object",
    properties: {
      cell: { title: "Cell", type: "integer", minimum: 1, maximum: 9 },
      side: { title: "Side", enum: ["X", "O"] },
      wide: { title: "Wide", type: "integer", minimum: 1, maximum: 13 },
      free: { title: "Free", type: "integer" },
      ok: { title: "OK", type: "boolean" }
    },
    required: ["cell"]
  });
  const html = (values = { cell: "", side: "", wide: "", free: "", ok: false }) =>
    stripComments(render(SchemaForm, { props: { fields, values, idPrefix: "t" } }).body);
  const group = (body, name) => body.match(new RegExp(`<div class="values[^"]*" role="radiogroup" aria-labelledby="t-${name}-label"[\\s\\S]*?</div>`))?.[0] ?? "";
  const radios = (markup) => markup.match(/<button[^>]*role="radio"[^>]*>/g) ?? [];

  it("draws an enum and an integer spanning a dozen values or fewer as hairline buttons in a labelled group", () => {
    const body = html();
    const cell = group(body, "cell");
    assert.deepEqual([...cell.matchAll(/class="value fade-end[^"]*" dir="auto">([^<]*)</g)].map((m) => m[1]), ["1", "2", "3", "4", "5", "6", "7", "8", "9"]);
    assert.match(cell, /aria-required="true"/);
    assert.match(body, /<span class="kicker[^"]*" id="t-cell-label">Cell/, "the field's title names the group");
    for (const radio of radios(cell)) assert.match(radio, /class="chip [^"]*\bquiet\b[^"]*\binteractive\b/, "hairline = pressable");
    assert.equal(radios(group(body, "side")).length, 2);
    assert.doesNotMatch(body, /<input[^>]*id="t-cell"/, "no box to type the cell in");
  });

  it("keeps a free number, a range past a dozen, and a boolean as they were", () => {
    const body = html();
    assert.match(body, /<input id="t-wide"[^>]*type="number"/);
    assert.match(body, /<input id="t-free"[^>]*type="number"/);
    assert.match(body, /<input id="t-ok"[^>]*type="checkbox"/);
  });

  it("selects with a tint, one tab stop per group, and nothing chosen for the reader", () => {
    const none = radios(group(html(), "cell"));
    assert.ok(none.every((radio) => /aria-checked="false"/.test(radio)) && none.every((radio) => !/\bactive\b/.test(radio)));
    assert.deepEqual(none.map((radio) => radio.match(/tabindex="(-?\d)"/)[1]), ["0", "-1", "-1", "-1", "-1", "-1", "-1", "-1", "-1"]);
    const five = radios(group(html({ cell: "5", side: "", wide: "", free: "", ok: false }), "cell"));
    assert.match(five[4], /\bactive\b[^>]*aria-checked="true"[^>]*tabindex="0"|aria-checked="true"[^>]*tabindex="0"/);
    assert.match(five[4], /class="chip [^"]*\bactive\b/, "selection = tint");
    assert.match(five[0], /tabindex="-1"/);
  });

  // A press only selects, so a mis-click is fixed by pressing again; the form's Send sends.
  it("selects on a press and sends only from Send or Enter, with arrows moving as radios do", () => {
    const source = readFileSync(new URL("./SchemaForm.svelte", import.meta.url), "utf8");
    assert.match(source, /onclick=\{\(\) => pick\(field, value\)\}/);
    assert.doesNotMatch(source.match(/function pick[\s\S]*?\n {2}\}/)[0], /submit/);
    const keys = source.match(/function stepValues[\s\S]*?\n {2}\}/)[0];
    assert.match(keys, /event\.key === "Enter"[\s\S]*?requestSubmit\(\)/);
    assert.match(keys, /ArrowRight[\s\S]*ArrowDown[\s\S]*ArrowLeft[\s\S]*ArrowUp/);
    assert.match(keys, /Home[\s\S]*End/);
    assert.match(source, /:global\(\.chip\) \{\s*min-width: var\(--control-height\);/);
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

  it("sums up the problems in one line", () => {
    const problems = validate(fields, { title: "", count: "0", address: { city: "" } });
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

describe("schema constraints", () => {
  const fields = describeSchema({
    type: "object",
    properties: {
      player_name: { type: "string", minLength: 1, maxLength: 40 },
      code: { anyOf: [{ type: "string", pattern: "^[A-Z]{3}$" }, { type: "null" }] },
      tags: { type: "array", items: { type: "string" }, minItems: 1, maxItems: 2 }
    },
    required: ["player_name", "tags"]
  });
  const ok = { player_name: "Ada", code: "", tags: ["x"] };

  it("marks required inputs, and focuses the first marked field on a refused submit", async () => {
    const { body } = render(SchemaForm, { props: { fields, values: ok, idPrefix: "c", errors: { tags: "Add at least 1 entry." } } });
    assert.match(body.match(/<input[^>]*id="c-player_name"[^>]*>/)?.[0] ?? "", /aria-required="true"/);
    assert.doesNotMatch(body.match(/<input[^>]*id="c-code"[^>]*>/)?.[0] ?? "", /aria-required/);
    const add = body.match(/<button[^>]*class="[^"]*\brow-add\b[^"]*"[^>]*>/)?.[0] ?? "";
    assert.match(add, /data-invalid=""/);
    assert.match(add, /aria-describedby="c-tags-error"/);

    let focused = null;
    const field = { focus: () => (focused = field) };
    const form = { querySelector: (selector) => (selector === '[aria-invalid="true"], [data-invalid]' ? field : null) };
    await focusFirstProblem(form);
    assert.equal(focused, field);
  });
});
