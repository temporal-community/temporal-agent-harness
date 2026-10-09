// ABOUTME: Pins how a structured handler reply renders in the chat pane. It is data, so it is a
// monospace block that keeps its indentation, with the copy control in its header beside the JSON
// tag; a long payload folds to a few lines behind a real "Show all" toggle instead of taking over
// the pane, a short one shows whole with no toggle, and copy puts the handler's exact JSON on the
// clipboard rather than the highlighted markup. Folded, only the folded lines are drawn; past the
// highlight limit the payload is plain text; an empty result is a note, not an empty box; and the
// payload reads left to right on a right-to-left page.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import { HIGHLIGHT_LIMIT } from "../../highlight.ts";
import JsonReply, {
  COLLAPSED_LINES,
  DRAWN_CHARS,
  FOLD_AFTER_LINES,
  atEnd,
  drawnPart,
  headLines,
  prettyJson
} from "./JsonReply.svelte";

const record = (n) => ({ name: `Person ${n}`, email: `user${n}@acme.test`, company: `Acme ${n}`, status: "active" });
const markup = (props) => stripComments(render(JsonReply, { props }).body);
/** A JSON array that pretty-prints to exactly `lines` lines. */
const linesOf = (lines) => Array.from({ length: lines - 2 }, (_, n) => n);
const bodyOf = (html) => html.match(/<div class="json-body[^>]*>([\s\S]*?)<\/div>\s*(?:<button|<\/div>\s*$)/)?.[1] ?? "";

describe("a structured reply", () => {
  it("folds a long payload behind a toggle", () => {
    const json = { records: Array.from({ length: 40 }, (_, n) => record(n)), skipped: [] };
    const body = markup({ json });
    assert.match(body, /class="json-reply[^"]*collapsed/);
    assert.match(body, /<button[^>]*class="json-toggle[^"]*"[^>]*aria-expanded="false"[^>]*>[\s\S]*?Show all 245 lines/);
  });

  it("folds only past the threshold, so a fold always hides a few lines", () => {
    assert.equal(FOLD_AFTER_LINES, 14);
    assert.doesNotMatch(markup({ json: linesOf(14) }), /collapsed|json-toggle/);
    assert.match(markup({ json: linesOf(15) }), /Show all 15 lines/);
  });

  it("formats the line count for the reader's locale", () => {
    assert.match(markup({ json: linesOf(13_046) }), new RegExp(`Show all ${(13_046).toLocaleString()} lines`));
  });

  it("heads the block with copy then the JSON tag, so the tag holds the right edge", () => {
    const body = markup({ json: linesOf(40) });
    const head = body.match(/<div class="json-head[^"]*">([\s\S]*?)<\/div>\s*<div class="json-body/);
    assert.ok(head, "a header row above the code");
    assert.match(head[1], /aria-label="Copy JSON"[\s\S]*class="code-language-tag[^"]*">JSON<\/span>\s*$/);
    assert.match(head[1], /<button[^>]*data-tip-align="end"/, "its tip grows inward");
  });

  it("streams unparsed and unhighlighted in the same block, with only the tag in its header", () => {
    const body = markup({ text: '{"records":[{"name":"Dana' });
    assert.match(body, /class="json-reply[^"]*streaming/);
    assert.match(body, /<div class="json-head[^"]*">\s*<span class="code-language-tag[^"]*">JSON<\/span>\s*<\/div>/);
    assert.doesNotMatch(body, /Copy JSON|json-toggle|md-syntax/);
  });

  it("streams a fenced reply without its fence", () => {
    const body = markup({ text: '```json\n{"records": [' });
    assert.doesNotMatch(body, /```/);
    assert.match(body, /<code[^>]*>\{&quot;records&quot;: \[|<code[^>]*>\{"records": \[/);
  });

  /* A scroller inside redrawn markup came back at the top on every delta and every scrub step,
     so the newest streamed JSON could never be read. The scroller has to be this component's own
     element, around the block. */
  it("scrolls in its own element, outside the markup it redraws", () => {
    assert.match(markup({ text: '{"records":[' }), /<div class="json-body[^>]*>\s*<pre class="json-code/);
    assert.match(markup({ json: { a: 1 } }), /<div class="json-body[^>]*>\s*<pre class="json-code[^>]*><code[^>]*>[\s\S]*md-syntax-property/);
  });

  it("names the scroller and keeps the payload left to right on an RTL page", () => {
    const body = markup({ json: { a: 1 } });
    assert.match(body, /<div class="json-body[^"]*"[^>]*dir="ltr"[^>]*role="region"[^>]*aria-label="JSON reply"/);
    assert.doesNotMatch(body, /json-body[^>]*tabindex/, "a scroller is a keyboard stop on its own; a short one is not");
  });

  it("tails the stream only while the reader is at its end", () => {
    assert.equal(atEnd({ scrollHeight: 1000, clientHeight: 200, scrollTop: 800 }), true);
    assert.equal(atEnd({ scrollHeight: 1000, clientHeight: 200, scrollTop: 798.5 }), true, "fractional line boxes");
    assert.equal(atEnd({ scrollHeight: 1000, clientHeight: 200, scrollTop: 400 }), false, "scrolled up to read");
  });

  it("copies the exact payload, never the folded or highlighted text", () => {
    const json = { records: [record(1)], skipped: ["3. ??? no contact info"], note: "<b>不</b> שלום 👩‍💻" };
    assert.deepEqual(JSON.parse(prettyJson(json)), json);
    assert.match(markup({ json }), /<span class="copyable[\s\S]*?aria-label="Copy JSON"/, "the shared copy primitive");
    const source = readFileSync(new URL("./JsonReply.svelte", import.meta.url), "utf8");
    assert.match(source, /<Copyable value=\{body\}/, "copy reads the whole payload, not what is drawn");
  });

  it("shows an empty result as a note, still with its copy", () => {
    for (const json of [{}, []]) {
      const body = markup({ json });
      assert.match(body, /class="json-reply[^"]*empty/);
      assert.match(body, /<span class="json-empty[^"]*">Empty result<\/span>/);
      assert.match(body, /aria-label="Copy JSON"/);
      assert.doesNotMatch(body, /json-body/, "no near-empty box");
    }
  });
});

describe("a large structured reply", () => {
  const huge = { records: Array.from({ length: 1_000 }, (_, n) => ({ ...record(n), notes: "x".repeat(900) })) };
  const pretty = JSON.stringify(huge, null, 2);

  it("draws only the folded lines while folded", () => {
    const code = bodyOf(markup({ json: huge }));
    assert.equal(code.split("\n").length, COLLAPSED_LINES, "the folded lines, not the whole payload clipped by CSS");
    assert.ok(code.length < 10_000, `folded markup is ${code.length} chars`);
  });

  it("still copies all of it", () => {
    assert.deepEqual(JSON.parse(prettyJson(huge)), huge);
  });

  it("finds the folded lines without splitting the whole payload", () => {
    assert.equal(headLines(pretty, COLLAPSED_LINES), pretty.split("\n").slice(0, COLLAPSED_LINES).join("\n"));
    assert.equal(headLines("a\nb", COLLAPSED_LINES), "a\nb");
  });

  /* Laying out the whole megabyte on every delta and scrub step cost most of a second each. */
  it("draws only the tail of a stream and the head of a finished text", () => {
    const tail = drawnPart(pretty, true);
    const head = drawnPart(pretty, false);
    assert.ok(tail.startsWith("…") && pretty.endsWith(tail.slice(1)) && tail.length <= DRAWN_CHARS + 1);
    assert.ok(head.endsWith("\n…") && pretty.startsWith(head.slice(0, -2)) && head.length <= DRAWN_CHARS + 2);
    assert.equal(drawnPart("{}", true), "{}");
    assert.match(markup({ text: pretty }), /<code[^>]*>…/, "the streaming block draws the tail");
  });

  it("is plain text past the highlight limit", () => {
    assert.ok(pretty.length > HIGHLIGHT_LIMIT.chars);
    /* Expanded is client state, so render the case SSR can reach: a payload that does not fold
       but is past the limit by size alone — one enormous line. */
    const body = markup({ json: { blob: "y".repeat(HIGHLIGHT_LIMIT.chars + 1) } });
    assert.match(body, /<pre class="json-code/);
    assert.doesNotMatch(body, /md-syntax/);
  });
});
