// ABOUTME: Pins TraceRow, the one line the chat's turn trace and the Logs pane both draw. A row
// with details is the button that opens them, its glyph doubling as the caret; a row without is
// no button at all. Hue is the glyph's and the status word's only, and depth indents the row's
// line but not its details.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRawSnippet } from "svelte";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import ReplyStatus from "../agent/ReplyStatus.svelte";
import TraceRow from "./TraceRow.svelte";

const source = readFileSync(new URL("./TraceRow.svelte", import.meta.url), "utf8");
const details = createRawSnippet(() => ({ render: () => "<p class=\"probe-details\">details</p>" }));
const html = (props) => stripComments(render(TraceRow, { props: { children: details, ...props } }).body);
const rule = (selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return source.match(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`))?.[1] ?? "";
};

describe("TraceRow", () => {
  it("makes an expandable row one button with aria-expanded and the disclosure chevron", () => {
    const closed = html({ name: "open_trip", expandable: true, controls: "row-1-details", onToggle: () => {} });
    const buttons = closed.match(/<button\b[^>]*>/g) ?? [];
    assert.equal(buttons.length, 1);
    assert.match(buttons[0], /class="trace-line disclosure[^"]*"/);
    assert.match(buttons[0], /aria-expanded="false"/);
    assert.match(buttons[0], /aria-controls="row-1-details"/);
    assert.match(closed, /<svg[^>]*class="[^"]*\bdisclosure-chevron\b/);
    assert.doesNotMatch(closed, /probe-details/, "a closed row draws no details");

    const open = html({ name: "open_trip", expandable: true, expanded: true });
    assert.match(open, /aria-expanded="true"/);
    assert.match(open, /probe-details/);
  });

  it("draws no button, chevron or details for a row with nothing to open", () => {
    const plain = html({ name: "gemini-3.8-flash", note: "2,700 tok", meta: "4.6s", expanded: true });
    assert.doesNotMatch(plain, /<button|aria-expanded|disclosure|<svg[^>]*chevron|probe-details/);
    assert.match(plain, /<div class="trace-line[^"]*"/);
  });

  it("indents a nested row's line", () => {
    assert.match(html({ name: "read_trip_board", depth: 1 }), /class="trace-row[^"]*\bnested\b/);
    assert.doesNotMatch(html({ name: "run_travel_script" }), /class="trace-row[^"]*\bnested\b/);
    assert.match(rule(".trace-row.nested > .trace-line"), /margin-inline-start: calc\(var\(--depth\) \* 20px\);/);
    assert.match(html({ name: "search_flights", depth: 3 }), /--depth: 3/);
    assert.match(html({ name: "search_flights", depth: 40 }), /--depth: 8/, "a deep row keeps the cap's indent");
  });

  /* Indenting the whole row stepped its details in with it: three levels down, the Logs pane's
     Full details block started 95px in and a workflow id wrapped over three lines. */
  it("starts a row's details at one edge whatever the row's depth", () => {
    const depthRules = [...source.matchAll(/\n\s*([^{}\n]+) \{([^}]*)\}/g)].filter(([, , body]) => /--depth/.test(body));
    assert.ok(depthRules.length > 0);
    for (const [, selector] of depthRules) assert.match(selector, /> \.trace-line$/, `${selector} indents only the line`);
    for (const depth of [1, 3, 5, 40]) {
      const open = html({ name: "search_flights", depth, expandable: true, expanded: true });
      assert.match(open, /<\/button>\s*<p class="probe-details">/, `depth ${depth}: details sit beside the indented line, not in it`);
    }
    const logs = readFileSync(new URL("../agent/TranscriptPanel.svelte", import.meta.url), "utf8");
    const lineDetails = logs.match(/\n\s*\.line-details \{([^}]*)\}/)?.[1] ?? "";
    const turnHead = logs.match(/\n\s*\.turn-head \{([^}]*)\}/)?.[1] ?? "";
    assert.match(lineDetails, /padding: 0 var\(--gap-md\) var\(--gap-md\);/, "the Logs details sit at the turn's inset");
    assert.match(turnHead, /padding: 6px var\(--gap-md\) 8px;/, "which is the turn header's");
    assert.doesNotMatch(lineDetails, /depth/);
  });

  it("colours only the glyph and the status word, by the glyph's state", () => {
    const row = html({ glyph: "approval", name: "search_hotels", status: "jev checking", note: "approved" });
    assert.match(row, /class="trace-row[^"]*\bglyph-approval\b/);
    assert.match(row, /class="state-glyph/);
    assert.match(row, /<span class="trace-status[^"]*">jev checking<\/span>/);
    assert.match(rule(".glyph-approval"), /--status-color: var\(--live\);/);
    assert.match(rule(".glyph-error"), /--status-color: var\(--error\);/);
    assert.match(rule(".trace-status"), /color: var\(--status-color\);/);
    for (const muted of [".trace-note", ".trace-meta"]) {
      assert.match(rule(muted), /color: var\(--text-3\);/, `${muted} is muted`);
    }
    assert.doesNotMatch(html({ name: "Turn started" }), /state-glyph/, "no state, no glyph");
    assert.match(
      html({ glyph: "complete", prefix: "Handler reply", glyphLabel: "complete" }),
      /Handler reply<\/span>[\s\S]*<span class="visually-hidden">complete<\/span>/,
      "the state the glyph shows is read out, after the words"
    );
  });

  /* One line at every width, so a collapsed row keeps one height: the note gives way first,
     then the name, and the status word never does. */
  it("keeps a row on one line, cutting its note before its name", () => {
    assert.match(rule(".trace-copy"), /white-space: nowrap;/);
    assert.match(rule(".trace-note"), /flex: 1 1 0;/);
    assert.match(rule("strong"), /flex: 0 1 auto;/);
    assert.match(rule(".trace-status"), /flex: none;/);
  });

  it("draws a figure that never gives way, in tabular digits, before the note", () => {
    assert.match(html({ name: "gemini", figure: "4,097 tok", note: "x" }), /<span class="trace-figure[^"]*">4,097 tok<\/span>\s*<span class="trace-note/);
    assert.match(rule(".trace-figure"), /flex: none;[\s\S]*font-variant-numeric: tabular-nums;/);
  });

  it("writes the name in mono only for an identifier, and the trailing figure in tabular digits", () => {
    assert.match(html({ name: "open_trip", mono: true }), /<strong class="mono[^"]*"/);
    assert.doesNotMatch(html({ name: "State changed" }), /<strong class="mono/);
    assert.match(rule(".trace-meta"), /font-variant-numeric: tabular-nums;/);
    assert.match(html({ name: "x", meta: "01:05:09 PM" }), /<span class="trace-meta[^"]*" dir="auto">01:05:09 PM<\/span>/);
  });

  /* The caret sat at the row's far end, apart from the glyph the eye starts at. It is the glyph
     cell itself now: the glyph at rest, a right caret when pointed at or focused, a down caret
     while open, and a running glyph keeps its cell at rest even open. */
  it("turns an expandable row's glyph into its caret, and leaves a plain row's glyph alone", () => {
    const cell = (body) => body.match(/<span class="trace-glyph">([\s\S]*?)<\/span>\s*<span class="trace-copy/)?.[1] ?? "";
    const open = cell(html({ glyph: "complete", name: "open_trip", expandable: true }));
    assert.match(open, /class="state-glyph[\s\S]*<svg[^>]*class="[^"]*\bdisclosure-chevron\b/, "glyph then caret, one cell");
    assert.match(open, /lucide-chevron-right/);
    assert.doesNotMatch(cell(html({ glyph: "complete", name: "gemini" })), /chevron/);
    assert.match(html({ glyph: "complete", running: true, name: "x", expandable: true }), /<button[^>]*class="trace-line disclosure live/);

    const app = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    const at = (selector) => {
      const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return app.match(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`))?.[1] ?? "";
    };
    const glyph = ".trace-glyph > :not(.disclosure-chevron)";
    assert.match(at(".trace-glyph > *"), /grid-area: 1 \/ 1;/, "glyph and caret stack in the 15px cell");
    assert.match(at(".disclosure-chevron"), /opacity: 0;/);
    const shown = `.disclosure:focus-visible .disclosure-chevron,\n.disclosure[aria-expanded="true"]:not(.live) .disclosure-chevron`;
    assert.match(at(shown), /opacity: 1;/);
    assert.match(at(`.disclosure:focus-visible ${glyph},\n.disclosure[aria-expanded="true"]:not(.live) ${glyph}`), /opacity: 0;/);
    assert.match(at(`.disclosure:hover ${glyph}`), /opacity: 0;/);
    assert.match(at('.disclosure[aria-expanded="true"] .disclosure-chevron'), /rotate: 90deg;/);
    assert.match(at(".disclosure-chevron:dir(rtl)"), /scale: -1 1;/);
    assert.match(at('.disclosure[aria-expanded="true"] .disclosure-chevron:dir(rtl)'), /rotate: -90deg;/);
    assert.match(app, /@media \(prefers-reduced-motion: reduce\) \{\s*\.disclosure-chevron,\s*\.disclosure \.trace-glyph > \* \{\s*transition: none;/);
  });

  it("draws media under the line whether or not the row is open", () => {
    const media = createRawSnippet(() => ({ render: () => "<p class=\"probe-media\">media</p>" }));
    const closed = html({ name: "image_generation", expandable: true, media });
    assert.match(closed, /probe-media/);
    assert.doesNotMatch(closed, /probe-details/);
    assert.match(html({ name: "image_generation", media }), /probe-media/);
  });
});

/* The turn header's thinking loader was a 15px grid of 4px cells at the row's edge, larger than
   the glyphs below it and 1.5px off their column, so the tree rule looked shifted. It is a glyph
   in the same cell now: 11px, centered where the glyphs and the rule are. */
describe("the trace's glyph column", () => {
  const app = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
  const chat = readFileSync(new URL("../agent/AgentChatPanel.svelte", import.meta.url), "utf8");
  const status = readFileSync(new URL("../agent/ReplyStatus.svelte", import.meta.url), "utf8");
  const ruleIn = (css, selector) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return css.match(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`))?.[1] ?? "";
  };

  it("is one shared cell, used by the rows and the working loader alike", () => {
    const cell = ruleIn(app, ".trace-glyph");
    assert.match(cell, /width: 15px;/);
    assert.match(cell, /margin-inline-start: 1px;/);
    assert.match(cell, /place-items: center;/);
    assert.doesNotMatch(source, /\n\s*\.trace-glyph \{/, "TraceRow keeps no cell of its own");
    assert.doesNotMatch(status, /\n\s*\.trace-glyph \{/, "nor does the loader");
    assert.match(html({ name: "x" }), /<span class="trace-glyph">/);
    const loader = stripComments(render(ReplyStatus, { props: { label: "Thinking", startedAt: null, now: null } }).body);
    assert.match(loader, /<span class="trace-glyph[^"]*">\s*<span class="pixels[^"]*">/);
  });

  it("draws the loader at a glyph's weight", () => {
    const pixels = ruleIn(status, ".pixels");
    assert.match(pixels, /grid-template-columns: repeat\(3, 3px\);/);
    assert.match(pixels, /grid-auto-rows: 3px;/);
    assert.match(pixels, /gap: 1px;/);
  });

  /* 1px inset plus half of 15px is 8.5px, the middle of the 1px rule drawn 8px past the rows'
     start (14px into the list, whose 6px padding the rows sit inside). */
  it("centers the cell on the tree rule", () => {
    const list = ruleIn(chat, ".activity-list");
    assert.match(list, /padding: 4px 6px;/);
    assert.match(list, /no-repeat 14px 0 \/ 1px 100%;/);
    assert.match(ruleIn(chat, ".activity-list:dir(rtl)"), /background-position: calc\(100% - 14px\) 0;/);
  });
});
