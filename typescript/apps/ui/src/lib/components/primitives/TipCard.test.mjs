// ABOUTME: Pins the rich tip every multi-fact hint shares. It draws only the facts it is given a
// value for, never a placeholder; an identifier past two lines keeps both ends rather than being
// cut off at the end; and no value, however long or in whatever script, can run out of the card.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import TipCard, { MONO_MAX, middle, shownFields } from "./TipCard.svelte";

const html = (props) => stripComments(render(TipCard, { props }).body);
const source = readFileSync(new URL("./TipCard.svelte", import.meta.url), "utf8");

describe("TipCard", () => {
  it("leaves out a fact with no value instead of drawing a placeholder", () => {
    const fields = [
      { key: "Agent", value: null },
      { key: "Started", value: "" },
      { key: "Model", value: "   " },
      { key: "Tool", value: undefined },
      { key: "Turn", value: "0" }
    ];
    assert.deepEqual(shownFields(fields).map((field) => field.key), ["Turn"], "zero is a value");
    const body = html({ fields });
    assert.doesNotMatch(body, /Agent|Started|Model|Tool|—|Unknown/);
    assert.equal(body.match(/class="tip-card-key/g)?.length, 1);
    assert.doesNotMatch(html({ heading: "  ", fields }), /tip-card-heading/, "no heading, no empty line");
    assert.doesNotMatch(html({ body: "", fields }), /tip-card-body/);
  });

  it("renders markup-looking values as text", () => {
    const body = html({ heading: "<script>alert(1)</script>", fields: [{ key: "Note", value: "**bold** <b>x</b>" }] });
    assert.doesNotMatch(body, /<script>|<b>/);
    assert.match(body, /&lt;script>/);
  });

  /* A 1,000-character workflow id ran the tip's eight lines out, and the end — where ids differ — was what got cut. */
  it("keeps both ends of a long identifier, whole graphemes only", () => {
    const id = `agent-session-992d1d17-7953-45d8-b4f4-0ed6e3303391-${"9f8e7d6c5b4a".repeat(80)}`;
    const shown = middle(id);
    assert.equal([...shown].length, MONO_MAX);
    assert.ok(shown.startsWith("agent-session-992d1d17") && shown.endsWith(id.slice(-20)) && shown.includes("…"));
    assert.equal(middle("agent-session-992d1d17-7953-45d8-b4f4-0ed6e3303391"), "agent-session-992d1d17-7953-45d8-b4f4-0ed6e3303391", "a real id shows whole");
    const emoji = "👩🏽‍💻".repeat(100);
    assert.ok(middle(emoji).split("…").every((side) => [...new Intl.Segmenter().segment(side)].every((g) => g.segment === "👩🏽‍💻")), "a ZWJ sequence is never split");
    assert.match(html({ fields: [{ key: "ID", value: id, mono: true }] }), /class="tip-card-value mono[^"]*" dir="auto">agent-session-992d1d17[^<]*…/);
  });

  it("wraps anything anywhere, in its own direction, and never past the card", () => {
    assert.match(source, /\.tip-card \{\s*grid-template-columns: auto minmax\(0, 1fr\);/, "the value column can shrink");
    assert.match(source, /\.tip-card-heading,\s*\.tip-card-value,\s*\.tip-card-body \{\s*min-width: 0;\s*overflow-wrap: anywhere;\s*unicode-bidi: plaintext;/);
    assert.match(source, /\.tip-card-value\.mono \{\s*font-family: var\(--font-mono\);\s*word-break: break-all;/);
    assert.match(source, /\.tip-card-value \{[^}]*font-variant-numeric: tabular-nums;/);
    assert.equal(html({ heading: "نور", fields: [{ key: "Agent", value: "王秀英" }], body: "x" }).match(/dir="auto"/g)?.length, 3);
  });

  /* A hairline box is what a pressable control looks like here; a tip is read, not pressed. */
  it("shares the plain tip's surface, with no hairline or shadow", () => {
    const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    const surface = css.match(/\n\[data-tip\]::after,\n\.tip-card \{([^}]*)\}/)?.[1] ?? "";
    assert.match(surface, /background: var\(--surface-3\);/);
    assert.doesNotMatch(surface, /border|box-shadow/);
    assert.doesNotMatch(source, /border|box-shadow|#[0-9a-f]{3,8}\b|rgb\(/i, "no colors or boxes of its own");
  });
});
