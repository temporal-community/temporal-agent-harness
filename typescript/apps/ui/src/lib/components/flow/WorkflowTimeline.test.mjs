// ABOUTME: Keeps the run-wide timeline registered beside the waterfall and verifies that its
// shared-axis rows stay windowed, expose complete button names, and seek to each row's start.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { Bot, Cpu, MessageCircleQuestion, ShieldAlert, ShieldCheck, Wrench } from "@lucide/svelte";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { PANE_KINDS } from "$lib/panes/registry.ts";
import { buildStepTimeline, indexAtTimestamp } from "$lib/state/stepTimeline.ts";
import {
  buildWorkflowScale,
  projectTime,
  unprojectTime
} from "$lib/state/workflowTimelineScale.ts";
import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import WorkflowTimeline, {
  edgeSide,
  LABEL_WIDTH,
  labelColumnWidth,
  labelContentWidth,
  rowIcon,
  waitsOnPerson
} from "./WorkflowTimeline.svelte";

const source = readFileSync(new URL("./WorkflowTimeline.svelte", import.meta.url), "utf8");

/** A TipCard's spans are flat, so this drops a whole card: a tip is not printed text. */
const withoutTips = (html) =>
  html.replace(/<span class="tip-card [^"]*">(?:<span[^>]*>[^<]*<\/span>|\s)*<\/span>/g, "");

function timelineWith(rowCount) {
  const rows = Array.from({ length: rowCount }, (_, index) => ({
    key: `parent:1:span:${index}`,
    source: "span",
    kind: index % 2 ? "tool" : "model",
    label: `span ${index}`,
    tone: "done",
    startTs: index,
    endTs: index + 0.5,
    startIndex: index + 10,
    endIndex: index + 11,
    ongoing: false,
    parentTurnNumber: 1,
    workflowId: "root",
    depth: 0
  }));
  return {
    turns: [],
    workflow: {
      subagents: [],
      points: [[1, 0], [rowCount + 1, rowCount]],
      bounds: { startTs: 0, endTs: rowCount },
      gaps: [],
      rowCount,
      rowAt: (index) => rows[index]
    }
  };
}

describe("the run-wide timeline pane", () => {
  it("is registered directly before the latency waterfall", () => {
    assert.equal(PANE_KINDS[PANE_KINDS.indexOf("timeline") + 1], "latency");
  });

  it("renders one virtual row per workflow row without mounting the whole run", () => {
    const html = render(WorkflowTimeline, {
      props: { timeline: timelineWith(1_200), viewIndex: 0, onScrub() {} }
    }).body;
    const mounted = html.match(/data-workflow-row=/g)?.length ?? 0;
    assert.ok(mounted > 0, "the visible workflow rows render");
    assert.ok(mounted < 100, `windowing mounted ${mounted} of 1,200 rows`);
    assert.match(html, /height: 33600px/, "the virtual list reserves one 28px row per span");
  });

  it("marks its row scroller for the drawer's fit, with the rows one level down", () => {
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline: timelineWith(3), viewIndex: 0, onScrub() {} }
    }).body);
    assert.equal(html.match(/data-drawer-fit/g)?.length, 1);
    assert.match(html, /<div class="rows[^"]*" data-drawer-fit=""[^>]*><div class="windowed-rows/);
    /* Opened before the run has rows, the fit still needs something to watch for them. */
    const empty = render(WorkflowTimeline, { props: { timeline: timelineWith(0), viewIndex: 0, onScrub() {} } }).body;
    assert.match(empty, /class="empty[^"]*" data-drawer-fit=""/);
  });

  it("names marks with their span and duration and seeks to the span start", () => {
    const html = render(WorkflowTimeline, {
      props: { timeline: timelineWith(2), viewIndex: 0, onScrub() {} }
    }).body;
    assert.match(html, /aria-label="span 0 · 500ms"/);
    assert.match(source, /onclick=\{\(\) => onScrub\(row\.startIndex\)\}/);
  });

  it("prints each row's name once, in the label column, with its duration beside it", () => {
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline: timelineWith(2), viewIndex: 0, onScrub() {} }
    }).body);
    const row = withoutTips(html.match(/<div class="timeline-row[\s\S]*?data-workflow-row="parent:1:span:1"[\s\S]*?<\/button>/)[0]);
    assert.equal(row.match(/>span 1</g)?.length, 1, "the name is printed once");
    assert.match(row, /class="row-duration[^"]*">500ms</);
  });

  it("names each span beside it on the plot in a narrow pane only, hidden from screen readers", () => {
    // Unmeasured, the server render counts as narrow.
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline: timelineWith(2), viewIndex: 0, onScrub() {} }
    }).body);
    const labels = [...html.matchAll(/<span class="inline-label fade-end after[^"]*"[^>]*>([^<]*)<\/span>/g)];
    assert.deepEqual(labels.map((label) => label[1]), ["span 0", "span 1"], "the name alone, no duration");
    for (const [label] of labels) assert.match(label, /aria-hidden="true"/);

    assert.match(source, /const NARROW_PANE = 560;/);
    assert.match(source, /const inlineLabels = \$derived\(plotWidth < NARROW_PANE\);/, "a measured wide pane lays none out");
    assert.match(source, /\n {2}\.inline-label \{[^}]*display: none;/, "and shows none before it is measured");
    const compact = source.match(/@container timeline \(max-width: 559px\) \{[\s\S]*?\n {2}\}\n/)[0];
    assert.match(compact, /\.inline-label \{\s*display: block;/, "the narrow breakpoint shows them");
  });

  it("sizes the label column to its widest row, between 120px and 220px or 30% of the pane", () => {
    // A 6px-a-character face, the row's 8px padding, 4px gaps, a 14px caret and 12px a level.
    const metrics = { measure: (text) => 6 * text.length, padding: 8, gap: 4, caret: 14, indentStep: 12, indentCap: 8 };
    const fit = (rows, carets, pane = 900) => labelColumnWidth(labelContentWidth(rows, carets, metrics), pane);
    const attached = [
      { label: "gemini-3.8-flash", depth: 0, duration: "4.1s" },
      { label: "gemini-3.8-flash", depth: 0, duration: "4.6s" },
      { label: "approval · start_monty", depth: 0, duration: "1m 39s" }
    ];
    assert.equal(labelContentWidth(attached, false, metrics), 16 + 22 * 6 + 4 + 6 * 6, "name, one gap and duration");
    assert.equal(fit(attached, false), 188, "the screenshot's rows no longer leave a gutter at 220px");
    assert.equal(fit([{ label: "ask", depth: 0, duration: "1s" }], false), LABEL_WIDTH.min, "a short run keeps the floor");
    assert.equal(fit([{ label: "x".repeat(120), depth: 0, duration: "1m 39s" }], false), LABEL_WIDTH.max, "a long name hits the cap");
    assert.equal(fit([{ label: "x".repeat(120), depth: 0, duration: "1s" }], false, 600), 180, "or 30% of a smaller pane");

    const deep = [{ label: "fare_checker", depth: 3, duration: "2.5s", count: 12 }];
    assert.equal(labelContentWidth(deep, true, metrics), 16 + 36 + 14 + 72 + 18 + 24 + 3 * 4, "indent, caret and fold count count");
    assert.equal(
      labelContentWidth([{ label: "a", depth: 0, duration: "1m 11s" }], false, metrics),
      labelContentWidth([{ label: "a", depth: 0, duration: "9m 99s" }], false, metrics),
      "a ticking duration measures the same"
    );

    assert.match(source, /\.workflow-timeline \{[^}]*container-type: inline-size/);
    assert.match(source, /--label-width: clamp\(120px, 30cqi, 220px\);/, "the same clamp before the rows are measured");
    assert.match(source, /style:--label-width=\{labelColumn == null \? undefined : `\$\{labelColumn\}px`\}/);
    assert.match(source, /width: fresh \? width : Math\.max\(labelFit\.width, width\)/, "it only grows within one run");
  });

  it("fades a long name out at its end, not with an ellipsis, and never clips the duration", () => {
    const label = "approval check · jev_evaluator ".repeat(4).slice(0, 120);
    const timeline = timelineWith(1);
    timeline.workflow.rowAt(0).label = label;
    const html = stripComments(render(WorkflowTimeline, { props: { timeline, viewIndex: 0, onScrub() {} } }).body);
    const name = html.match(/<span class="row-name fade-end[^"]*" dir="auto">([^<]*)<\/span>/);
    assert.equal(name?.[1], label, "the whole name, faded by the shared utility");
    assert.ok(html.includes(`aria-label="${label} · 500ms"`), "the row's accessible name is whole");
    assert.equal(html.match(/tip-card-heading[^>]*>([^<]*)</)?.[1], label, "and so is its tip's heading");
    const nameRule = source.match(/\n {2}\.row-name \{([^}]*)\}/)[1];
    assert.doesNotMatch(nameRule, /text-overflow|overflow:/, "nothing here overrides .fade-end's clip");
    assert.match(nameRule, /text-align: match-parent;/);
    const duration = source.match(/\n {2}\.row-duration \{([^}]*)\}/)[1];
    assert.match(duration, /flex-shrink: 0;/);
    assert.match(duration, /font-variant-numeric: tabular-nums;/);
    assert.doesNotMatch(duration, /overflow|text-overflow|fade/, "the duration is never cut");
  });

  it("drops the label column in a narrow pane, naming the row in its lead cap's tip and its accessible name", () => {
    const compact = source.match(/@container timeline \(max-width: 559px\) \{[\s\S]*?\n {2}\}\n/)?.[0];
    assert.ok(compact, "the column goes at the narrow breakpoint");
    assert.match(
      compact,
      /\.plot-content \{\s*--label-width: calc\(var\(--pane-inset\) \+ min\(var\(--shown-depth\), 3\) \* 6px \+ var\(--carets\) \* var\(--caret\)\);/,
      "the plot takes the width, less a gutter of the pane inset, up to three levels of indent and the caret"
    );
    assert.match(compact, /\.timeline-row \{\s*--indent-step: 6px;\s*--indent-cap: 3;\s*\}/);
    assert.match(compact, /\.axis-title,\s*\.row-label > :not\(\.row-toggle\) \{\s*display: none;/, "no label text and no RUN heading, but the caret stays");
    assert.match(compact, /\.row-label \{\s*gap: 0;\s*padding-inline-end: 0;\s*\}/, "the caret keeps the wide label's inset and indent");
    assert.match(compact, /\.axis-label \{\s*padding: 0;/, "the head cell stays, unpadded, to measure the depth gutter");
    assert.match(source, /\.axis,\s*\.track \{\s*grid-column: 2;/, "the plot keeps its column with the label gone");
    assert.match(
      compact,
      /\.cap\.lead:hover > :global\(\.tip-card\),\s*\.timeline-row:focus-visible :is\(\.cap\.lead, \.edge-label\) > :global\(\.tip-card\) \{\s*display: grid;/,
      "the lead cap's tip shows on hover and when its row has keyboard focus"
    );
    assert.match(source, /\n {2}\.cap\.lead::before,\s*\.cap\.lead > :global\(\.tip-card\) \{\s*display: none;/, "a wide pane's label column names it instead");
    assert.match(source, /placeTip\(tip\)/, "a tip shown for its focused row is measured, as a hovered one is");
    assert.match(source, /\.track \{[^}]*overflow: clip visible;/, "the tip can rise out of its row");
    assert.doesNotMatch(source, /\n {2}\.cap \{[^}]*overflow: hidden/, "nor is it clipped by its cap");

    const label = "approval check · jev_evaluator · 航班预订 · حجز الرحلة ".repeat(3).slice(0, 120);
    const timeline = timelineWith(1);
    timeline.workflow.rowAt(0).label = label;
    const html = stripComments(render(WorkflowTimeline, { props: { timeline, viewIndex: 0, onScrub() {} } }).body);
    const row = html.match(/<div class="timeline-row[^>]*>/)[0];
    assert.ok(row.includes(`aria-label="${label} · 500ms"`), "the row's accessible name is its full name and duration");
    assert.match(row, /tabindex="0"/, "the row still takes focus");
    const lead = html.match(/<span class="cap start lead[^"]*" data-tip="">[\s\S]*?<\/span> <\/span>/)?.[0];
    assert.ok(lead, "the start cap carries the row's tip");
    assert.equal(lead.match(/tip-card-heading[^>]*>([^<]*)</)?.[1], label, "headed with the full name");
    assert.match(lead, /tip-card-key[^>]*>Status<\/span> <span[^>]*>Done</);
    assert.match(lead, /tip-card-key[^>]*>Start<\/span> <span[^>]*>\+0s</);
    assert.match(lead, /tip-card-key[^>]*>Duration<\/span> <span[^>]*>500ms</);
  });

  /* A few rows in a tall narrow pane showed a pane-high rule beside the caret gutter, down
     empty space, and carets flush with the pane's edge. */
  it("ends every rule at the last row and insets the first column by the pane inset", () => {
    const html = stripComments(render(WorkflowTimeline, { props: { timeline: timelineWith(3), viewIndex: 0, onScrub() {} } }).body);
    const [beforeRows, ...rows] = html.split('<div class="timeline-row');
    const rowsLayer = beforeRows.slice(beforeRows.indexOf('class="rows"'));
    assert.doesNotMatch(rowsLayer, /run-rule|style="[^"]*height:/, "nothing in the rows scroller outside a row, nor sized to it");
    for (const row of rows) assert.equal(row.match(/class="run-rule/g)?.length, 2, "the run's first and last pixel, in each row");
    assert.match(source, /\n {2}\.run-rule \{[^}]*inset-block: 0;/, "a rule is its row's height");

    const compact = source.match(/@container timeline \(max-width: 559px\) \{[\s\S]*?\n {2}\}\n/)[0];
    assert.match(compact, /\.timeline-row::before \{\s*display: none;/, "no depth guides beside a narrow pane's carets");
    assert.match(source, /\n {2}\.row-label \{[^}]*padding-inline: calc\(var\(--pane-inset\) \+ var\(--indent\)\)/, "carets and names start at the pane inset");
    assert.doesNotMatch(compact, /\.row-label \{[^}]*padding-inline(-start)?:/, "a narrow pane keeps that inset");
    assert.match(compact, /--label-width: calc\(var\(--pane-inset\) \+/, "and its gutter makes room for it");
    assert.match(source, /\n {2}\.axis-label \{[^}]*padding: 0 var\(--pane-inset\);/, "the RUN heading lines up with the names");
  });

  /* A narrow pane has no label column, so a row zoomed or scrolled past its start was a blank
     line: no name, no bar, nothing to hover. */
  it("names a narrow pane's row at the plot's edge while its start is off the plot", () => {
    // A 300px plot scrolled 1,000px in, with 11px caps.
    assert.equal(edgeSide(900, 1_000, 300, 11), "before");
    assert.equal(edgeSide(1_400, 1_000, 300, 11), "after");
    assert.equal(edgeSide(995, 1_000, 300, 11), null, "a cap partly in view is its own label");
    assert.equal(edgeSide(1_305, 1_000, 300, 11), null);

    assert.match(source, /\{@const offscreen = inlineLabels \? offscreenSide\(row\) : null\}/, "only a narrow pane, every row");
    assert.match(source, /\{#if offscreen\}\s*<button[^>]*class=\{\["edge-label", offscreen\]\}[\s\S]*?\{@render rowTip\(row, rowIndex\)\}/, "with the row's tip");
    assert.match(source, /\{@const leadTip = inlineLabels && !offscreen \? "" : undefined\}/, "one tip a row, never two");
    const compact = source.match(/@container timeline \(max-width: 559px\) \{[\s\S]*?\n {2}\}\n/)[0];
    assert.match(compact, /\.edge-label \{\s*display: flex;/);
    assert.match(source, /\n {2}\.edge-label \{[^}]*display: none;[^}]*background: var\(--surface-1\);/, "hidden in a wide pane, a chip that reads over a lane");
    assert.match(source, /\.edge-label > :global\(svg\) \{\s*flex-shrink: 0;/);
    assert.match(source, /class="edge-name fade-end"/);
  });

  /* Under a drawer's top edge a gap's tip has no room above, so keepTipsInView flips it down;
     an axis that clips vertically hid it on both sides, a sliver under the transport. */
  it("lets a gap's tip out of the axis vertically, so it can flip below", () => {
    assert.match(source, /\.axis \{[^}]*overflow: clip visible;/);
    assert.doesNotMatch(source, /\.axis \{[^}]*overflow(-y)?: hidden/);
  });

  it("draws marks with no text in them, so nothing boxes over a bar", () => {
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline: timelineWith(2), viewIndex: 0, onScrub() {} }
    }).body);
    const marks = [...html.matchAll(/<button[^>]*class="mark [\s\S]*?<\/button>/g)].map(([mark]) => mark);
    assert.equal(marks.length, 2);
    for (const mark of marks) assert.doesNotMatch(withoutTips(mark), />[^<\s]/, "no text inside a mark");
    assert.doesNotMatch(source, /mark-text/);
  });

  it("keeps the run's first instant dot inside the plot", () => {
    const html = render(WorkflowTimeline, {
      props: { timeline: timelineWith(2), viewIndex: 0, onScrub() {} }
    }).body;
    assert.match(html, /class="mark [^"]*"[^>]*style="left: 0px;[^"]*--start-room: 0px/);
  });

  it("zooms by wheel, pinch and keys, with no zoom buttons in the head row", () => {
    const html = render(WorkflowTimeline, {
      props: { timeline: timelineWith(2), viewIndex: 0, onScrub() {} }
    }).body;
    for (const label of ["Zoom out", "Zoom in", "Fit"]) {
      assert.doesNotMatch(html, new RegExp(`aria-label="${label}"`));
    }
    assert.match(html, /class="axis-label[^"]*" data-tip="Zoom: ⌘\/Ctrl \+ scroll, \+ or −\nFit: 0"/);
    assert.match(html, /aria-keyshortcuts="\+ - 0"/);
    assert.match(source, /event\.ctrlKey.*event\.metaKey/s);
    assert.match(source, /event\.key === "\+"/);
    assert.match(source, /event\.key === "-"/);
    assert.match(source, /event\.key === "0"/);
  });

  it("draws each row kind with its own icon", () => {
    const kinds = [
      [{ kind: "model" }, Cpu],
      [{ kind: "tool" }, Wrench],
      [{ kind: "approval", waitType: "approval" }, ShieldAlert],
      [{ kind: "approval", waitType: "callback" }, MessageCircleQuestion],
      [{ kind: "evaluation", waitType: "evaluation" }, ShieldCheck],
      [{ kind: "subagent" }, Bot]
    ];
    for (const [row, icon] of kinds) assert.equal(rowIcon(row), icon, JSON.stringify(row));
    assert.throws(() => rowIcon({ kind: "workflow" }), /No icon/);
  });

  it("renders rows as a tree, with a chevron only on a row that has children", () => {
    const timeline = timelineWith(3);
    const rows = [0, 1, 2].map((index) => timeline.workflow.rowAt(index));
    rows[1].depth = 1;
    rows[2].depth = 2;
    rows[2].ongoing = true;
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline, viewIndex: 0, onScrub() {} }
    }).body);
    assert.match(html, /role="treegrid"/);
    assert.deepEqual([...html.matchAll(/aria-level="(\d+)"/g)].map(([, level]) => level), ["1", "2", "3"]);
    assert.equal(html.match(/aria-expanded="true"/g)?.length, 2, "the leaf has no expanded state");
    assert.equal(html.match(/<button[^>]*class="row-toggle/g)?.length, 2);
    assert.equal(html.match(/disclosure-chevron/g)?.length, 2);
    assert.equal(html.match(/<span class="row-toggle[^"]*" aria-hidden="true"><\/span>/g)?.length, 1, "a leaf keeps the caret's cell, so names line up");
  });

  it("draws the kind glyph in both caps and none in the label, with a failure's X at the end", () => {
    const timeline = timelineWith(3);
    timeline.workflow.rowAt(1).endTs = timeline.workflow.rowAt(1).startTs;
    timeline.workflow.rowAt(2).tone = "error";
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline, viewIndex: 0, onScrub() {} }
    }).body);
    for (const label of html.match(/<span class="row-label[\s\S]*?class="row-duration/g)) {
      assert.doesNotMatch(label, /<svg/, "the label column is text");
    }
    const caps = [...html.matchAll(/<span class="cap (start|end|point)[^"]*"[^>]*>([\s\S]*?)<\/span>/g)]
      .map(([, cap, inner]) => `${cap}:${inner.match(/lucide-([a-z-]+?)"/)?.[1] ?? "none"}`);
    assert.deepEqual(caps, ["start:cpu", "end:cpu", "point:wrench", "start:cpu", "end:circle-x"]);
    assert.doesNotMatch(source, /\.cap\.end \{[^}]*background/, "the end cap keeps the start cap's tinted fill");
    assert.doesNotMatch(source, /\.cap[^{]*\{[^}]*background: (?:transparent|none)/, "no cap is hollow");
  });

  it("folds a routine group to one row with its count and kind summary, and dims what is routine", () => {
    const timeline = timelineWith(6);
    const rows = [0, 1, 2, 3, 4, 5].map((index) => timeline.workflow.rowAt(index));
    rows[1].depth = 1;
    rows[2].depth = 1;
    rows[4].depth = 1;
    rows[5].tone = "error";
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline, viewIndex: 7, onScrub() {} }
    }).body);
    assert.deepEqual([...html.matchAll(/data-workflow-row="([^"]+)"/g)].map(([, key]) => key), ["parent:1:span:0", "parent:1:span:3", "parent:1:span:5"]);
    assert.match(html, /aria-expanded="false"/);
    assert.match(html, /class="row-count[^"]*">\+2<\/span>\s*<span class="row-summary fade-end[^"]*">1 tool · 1 model</);
    assert.match(html, /class="row-count[^"]*">\+1<\/span>\s*<span class="row-summary fade-end[^"]*">model</, "one kind is its noun, not the count twice");
    assert.match(html, /aria-label="span 0 · 500ms · 2 folded: 1 tool · 1 model"/, "the accessible name says what the fold hides");
    assert.match(html, /class="row-name fade-end summarized[^"]*"/, "the name keeps its width ahead of the summary");
    assert.match(source, /\.row-summary \{\s*flex: 1 1 0;\s*min-width: 0;/, "the summary takes only the room left over");
    assert.match(html, /class="timeline-row disclosure routine/);
    assert.match(html, /class="timeline-row signal/);
  });

  it("toggles a fold through the same caret both ways, in a wide pane and a narrow one", () => {
    const toggles = [...source.matchAll(/onclick=\{\(\) => toggleRow\(row\.key\)\}/g)];
    assert.equal(toggles.length, 1, "one control folds and unfolds");
    assert.match(source, /<button\s+type="button"\s+class="row-toggle"[^>]*aria-label=\{`\$\{collapsed \? "Expand" : "Collapse"\} \$\{row\.label\}`\}\s+onclick=\{\(\) => toggleRow\(row\.key\)\}/);
    assert.match(source, /folds\.set\(key, !tree\.collapsed\.has\(key\)\)/, "it flips what is shown, default or not");
    const compact = source.match(/@container timeline \(max-width: 559px\) \{[\s\S]*?\n {2}\}\n/)[0];
    assert.match(compact, /\.row-label > :not\(\.row-toggle\) \{\s*display: none;/, "a narrow pane hides the label's text and keeps its caret");
  });

  it("seeks from anywhere on a row's plot, a parent's caps and lane included, and never folds", () => {
    const mark = source.match(/<button\s+type="button"\s+class=\{markClass\(row\)\}[\s\S]*?<\/button>/)[0];
    assert.match(mark, /onclick=\{\(\) => onScrub\(row\.startIndex\)\}/);
    assert.doesNotMatch(mark, /toggleRow|onclick=[\s\S]*onclick=/, "the caps are inside the one mark button, with no handler of their own");
    const track = source.match(/<div\s+class="track"[\s\S]*?>/)[0];
    assert.match(track, /onpointerdown=\{startScrub\}/, "the lane, the band and the gap between scrub");
    assert.doesNotMatch(track, /toggleRow/);
  });

  it("keeps an open session's running spans on to now when a click seeks back", () => {
    const app = readFileSync(new URL("../../../App.svelte", import.meta.url), "utf8");
    const pane = app.match(/<WorkflowTimeline[\s\S]*?\/>/)[0];
    assert.match(pane, /live=\{!run\.session\?\.closed\}/, "seeking leaves following, not the session");
    assert.doesNotMatch(pane, /following/);
  });

  /* Three levels: a subagent, its child, and that child's own child, beside a sibling. */
  function nestedTimeline() {
    const timeline = timelineWith(5);
    timeline.workflow.bounds = { startTs: 0, endTs: 12 };
    const spans = [[0, 10, 0], [1, 5, 1], [2, 3, 2], [6, 9, 1], [10.5, 11, 0]];
    spans.forEach(([startTs, endTs, depth], index) => Object.assign(timeline.workflow.rowAt(index), { startTs, endTs, depth }));
    Object.assign(timeline.workflow.rowAt(0), { kind: "subagent", label: "researcher (sub-1)" });
    timeline.workflow.rowAt(2).ongoing = true;
    return timeline;
  }

  it("indents nested rows, draws a guide per ancestor and backs them with each parent's band", () => {
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline: nestedTimeline(), viewIndex: 0, onScrub() {} }
    }).body);
    const rows = html.split('<div class="timeline-row').slice(1);
    assert.deepEqual(rows.map((row) => row.match(/--depth: (\d+)/)[1]), ["0", "1", "2", "1", "0"]);
    assert.match(source, /padding-inline: calc\(var\(--pane-inset\) \+ var\(--indent\)\) var\(--gutter-tight\);/, "a step per level");
    assert.match(source, /--indent: calc\(min\(var\(--depth\), var\(--indent-cap\)\) \* var\(--indent-step\)\);/);
    assert.match(source, /\.timeline-row::before \{[^}]*width: var\(--indent\);[^}]*background: repeating-linear-gradient\(to right, var\(--guides\)\);/, "one guide per ancestor");
    assert.match(source, /--guides:[^;]*var\(--border\) calc\(var\(--guide-at\) - 0\.5px\) calc\(var\(--guide-at\) \+ 0\.5px\)/, "a 1px guide in the activity feed's --border");

    // 600px over 12s: 50px a second, and a band runs cap to cap, 11px past each end.
    const bands = rows.map((row) =>
      (row.match(/<span class="parent-band[^>]*>/g) ?? []).map((band) => {
        const [, left, width] = band.match(/left: (-?[\d.]+)px; width: ([\d.]+)px;/) ?? [];
        return `${left}+${width}`;
      })
    );
    assert.deepEqual(bands, [[], ["-11+522"], ["-11+522", "39+222"], ["-11+522"], []], "outermost first, one per ancestor");
    const band = source.match(/\n {2}\.parent-band \{([^}]*)\}/)[1];
    assert.match(band, /background: color-mix\(in srgb, var\(--surface-3\) 35%, transparent\);/, "a surface tint");
    assert.doesNotMatch(band, /border|outline|box-shadow/, "never a stroke, which would read as a control");
  });

  it("folds a parent to one row with its count, its caret and aria-expanded", () => {
    const timeline = nestedTimeline();
    timeline.workflow.rowAt(2).ongoing = false;
    /* Calls of a second each, so none is a long tail, and the cursor past the subagent's end. */
    Object.assign(timeline.workflow.rowAt(1), { endTs: 2 });
    Object.assign(timeline.workflow.rowAt(3), { endTs: 7 });
    timeline.workflow.points = [[1, 0], [13, 12]];
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline, viewIndex: 13, onScrub() {} }
    }).body);
    const rows = html.split('<div class="timeline-row').slice(1);
    assert.equal(rows.length, 2, "the subagent folds, with nothing under it worth the eye");
    assert.match(rows[0], /^ disclosure[^"]*"/);
    assert.match(rows[0], /aria-expanded="false"/);
    assert.match(rows[0], /aria-label="researcher \(sub-1\) · 10s · 2 folded: 2 tools"/, "its name says what the fold hides");
    assert.match(rows[0], /<button[^>]*class="row-toggle[^"]*"[^>]*aria-label="Expand researcher \(sub-1\)"[^>]*>\s*<svg[^>]*disclosure-chevron/);
    assert.match(rows[0], /class="row-count[^"]*">\+2</);
    assert.doesNotMatch(rows[0], /parent-band/);
    assert.doesNotMatch(rows[1], /aria-expanded/, "a leaf has no expanded state");
    assert.match(source, /if \(key === "ArrowRight" && collapsed\) toggleRow\(row\.key\);/, "→ opens a folded parent");
    assert.match(source, /else if \(key === "ArrowLeft" && hasChildren\(index\) && !collapsed\) toggleRow\(row\.key\);/, "← folds an open one");
    assert.match(source, /folds\.set\(key, !tree\.collapsed\.has\(key\)\)/, "the reader's fold is kept per parent");
  });

  it("draws the shared caret on every parent row, turned down while it is open", () => {
    const appCss = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    assert.match(appCss, /\.disclosure\[aria-expanded="true"\] \.disclosure-chevron \{\s*rotate: 90deg;/);
    assert.match(source, /class=\{\["timeline-row", expandable && "disclosure"/, "the row carries aria-expanded, which turns the caret");
    assert.match(source, /\.row-toggle > :global\(\.disclosure-chevron\) \{\s*opacity: 1;/, "with no glyph to swap with, the caret is always drawn");
  });

  it("folds an opened idle gap back from anywhere across the stretch it opened", () => {
    const timeline = timelineWith(2);
    timeline.workflow.gaps = [{ key: "idle:0.5:1", startTs: 0.5, endTs: 1 }];
    const html = render(WorkflowTimeline, { props: { timeline, viewIndex: 0, onScrub() {} } }).body;
    /* A sub-second gap was announced as "Expand 0s idle gap". */
    assert.match(html, /class="gap-toggle[^"]*"[^>]*aria-label="Expand 500ms idle gap" aria-expanded="false"/);
    assert.match(source, /expandedGapKeys = expandedGapKeys\.includes\(gap\.key\)\s*\? expandedGapKeys\.filter/);
    assert.match(source, /style:left=\{`\$\{expanded \? projectTime\(scale, gap\.startTs\) : gapCenter\(gap\)\}px`\}/);
    assert.match(source, /style:width=\{expanded \? `\$\{projectTime\(scale, gap\.endTs\) - projectTime\(scale, gap\.startTs\)\}px`/);
  });

  /* The reported monty run: calls at 22m, a subagent left open through 53 quiet minutes, more
     calls at 1h16m. The open subagent once held the whole stretch busy, so nothing folded and
     every 4–9s call was a ~1px cap. */
  function quietRun() {
    const at = (event, timestamp, data = {}) => ({
      event,
      data: { type: event, agent_id: "root", turn_number: 1, timestamp, ...data }
    });
    const call = (start, seconds) => [
      at("model_interaction_started", start, { model: `call-${start}` }),
      at("model_interaction_ended", start + seconds)
    ];
    const frames = [
      at("turn_started", 0),
      ...call(1320, 3.9),
      ...call(1324, 4.1),
      at("subagent_started", 1335, { subagent_id: "watch", agent_key: "watcher", workflow_id: "wf-watch" }),
      ...call(1336, 6.9),
      at("reply_delta", 3000, { delta: "." }),
      ...call(4570, 9.2),
      ...call(4581, 6),
      ...call(4590, 7.6)
    ];
    return { frames, timeline: buildStepTimeline(frames) };
  }

  const markBox = (html, label) => {
    const mark = html.match(new RegExp(`<button[^>]*class="mark [^"]*"[^>]*style="left: ([\\d.]+)px; width: ([\\d.]+)px;[^"]*"[^>]*aria-label="${label}`));
    assert.ok(mark, `a mark for ${label}`);
    return { left: Number(mark[1]), width: Number(mark[2]) };
  };

  it("draws a span open through a folded gap across its break, at its true duration", () => {
    const { frames, timeline } = quietRun();
    const gap = timeline.workflow.gaps.find((item) => item.startTs === 1342.9);
    assert.ok(gap, "the quiet stretch under the open subagent folds");
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline, viewIndex: frames.length, live: true, onScrub() {} }
    }).body);
    const scale = buildWorkflowScale(timeline.workflow.bounds, timeline.workflow.gaps, 600);
    const breakX = (projectTime(scale, gap.startTs) + projectTime(scale, gap.endTs)) / 2;
    const open = markBox(html, "watcher \\(watch\\) · 54m 23s · ongoing");
    assert.ok(open.left < breakX && open.left + open.width > breakX, "the lane runs through the ≀");
    assert.match(html, new RegExp(`class="gap-break[^"]*"[^>]*style="left: ${breakX}px;"`));
  });

  it("gives a short call after the fold its own start and end at a 900px pane", () => {
    const { timeline } = quietRun();
    const { bounds, gaps } = timeline.workflow;
    const width = (scale) => projectTime(scale, 4587) - projectTime(scale, 4581);
    const unfolded = width(buildWorkflowScale(bounds, [], 900));
    const folded = width(buildWorkflowScale(bounds, gaps, 900));
    assert.ok(unfolded < 2, `${unfolded}px: a 6s call was one cap`);
    assert.ok(folded > 44, `${folded}px: wide enough for two caps`);
    assert.ok(folded > unfolded * 30, `${unfolded}px → ${folded}px`);
  });

  it("hangs the tick after a fold away from its break, so the break cuts no label", () => {
    const { frames, timeline } = quietRun();
    const html = render(WorkflowTimeline, { props: { timeline, viewIndex: frames.length, onScrub() {} } }).body;
    const scale = buildWorkflowScale(timeline.workflow.bounds, timeline.workflow.gaps, 600);
    const afterFold = projectTime(scale, 1320);
    assert.match(html, new RegExp(`class="tick[^"]*" style="left: ${afterFold}px;">\\s*22m 00s`));
    assert.doesNotMatch(source, /\.tick \{[^}]*transform/);
  });

  it("pins the playhead to the break while the cursor is inside a fold", () => {
    const { frames, timeline } = quietRun();
    const viewIndex = frames.findIndex((frame) => frame.event === "reply_delta") + 1;
    const html = render(WorkflowTimeline, { props: { timeline, viewIndex, onScrub() {} } }).body;
    const scale = buildWorkflowScale(timeline.workflow.bounds, timeline.workflow.gaps, 600);
    const gap = scale.find((segment) => segment.collapsed && segment.startTs < 3000 && segment.endTs > 3000);
    assert.match(html, new RegExp(`class="playhead[^"]*"[^>]*style="left: ${(gap.startPx + gap.endPx) / 2}px;"`));
  });

  it("maps a scrub position through the piecewise scale to the nearest event", () => {
    const points = [[0, 0], [10, 20], [20, 100]];
    const scale = buildWorkflowScale(
      { startTs: 0, endTs: 100 },
      [{ key: "idle", startTs: 20, endTs: 80 }],
      500
    );
    assert.equal(indexAtTimestamp(points, unprojectTime(scale, projectTime(scale, 90))), 19);
  });

  it("places a cue and playhead at the same x for the same event", () => {
    const timeline = timelineWith(2);
    const html = render(WorkflowTimeline, {
      props: {
        timeline,
        viewIndex: 2,
        anomalyMarkers: [{ id: "cue", index: 2, turnNumber: 1, tone: "error", label: "Failure" }],
        onScrub() {}
      }
    }).body;
    const x = projectTime(buildWorkflowScale(timeline.workflow.bounds, [], 600), 1);
    assert.match(html, new RegExp(`class="playhead [^"]*"[^>]*style="left: ${x}px;"`));
    assert.match(html, new RegExp(`class="cue error [^"]*"[^>]*style="left: ${x}px;"`));
    assert.match(html, /aria-label="Jump to Failure, turn 1"/);
  });

  it("draws a duration as two caps and a point as one", () => {
    const timeline = timelineWith(2);
    const point = timeline.workflow.rowAt(1);
    point.endTs = point.startTs;
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline, viewIndex: 0, onScrub() {} }
    }).body);
    const marks = [...html.matchAll(/<button[^>]*class="mark [\s\S]*?<\/button>/g)].map(([mark]) => mark);
    assert.equal(marks.length, 2);
    assert.match(marks[0], /class="cap start[\s"]/);
    assert.match(marks[0], /class="cap end[\s"]/);
    assert.doesNotMatch(marks[0], /class="cap point[\s"]/);
    assert.match(marks[1], /class="cap point[\s"]/);
    assert.doesNotMatch(marks[1], /class="cap start[\s"]/);
    assert.equal(marks[1].match(/class="cap /g)?.length, 1);
  });

  it("draws a span narrower than two caps as its start cap alone, the error X if it failed", () => {
    const cap = Number(source.match(/--cap: (\d+)px;/)[1]);
    const timeline = timelineWith(3);
    timeline.workflow.bounds = { startTs: 0, endTs: 600 };
    const [short, failed, edge] = [0, 1, 2].map((index) => timeline.workflow.rowAt(index));
    Object.assign(short, { startTs: 100, endTs: 100 + (2 * cap - 1) });
    Object.assign(failed, { startTs: 200, endTs: 230, tone: "error" });
    Object.assign(edge, { startTs: 300, endTs: 300 + 2 * cap });
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline, viewIndex: 0, onScrub() {} }
    }).body);
    const marks = [...html.matchAll(/<button[^>]*class="mark [\s\S]*?<\/button>/g)].map(([mark]) => mark);
    for (const mark of marks.slice(0, 2)) {
      assert.equal(mark.match(/class="cap /g)?.length, 1, "one cap, not a start and end overlapping");
      assert.match(mark, /class="cap point lead[\s"]/);
      assert.doesNotMatch(mark, /class="connector/, "no stray line beside it");
    }
    assert.match(marks[0], /lucide-cpu/, "the start cap's glyph");
    assert.match(marks[1], /lucide-circle-x/, "a failed one is the error cap");
    assert.match(marks[2], /class="cap start lead[\s\S]*class="cap end[\s"]/, `${2 * cap}px is wide enough for two caps`);
  });

  it("draws a wait on a person as a tinted lane, and work as a solid one", () => {
    assert.deepEqual(
      ["model", "tool", "approval", "evaluation", "subagent"].filter((kind) => waitsOnPerson({ kind })),
      ["approval"],
      "approval gates and callbacks, which share the approval kind"
    );
    assert.throws(() => waitsOnPerson({ kind: "workflow" }), /Unknown timeline row kind/);
    const timeline = timelineWith(3);
    Object.assign(timeline.workflow.rowAt(1), { kind: "approval", waitType: "approval", tone: "approval" });
    Object.assign(timeline.workflow.rowAt(2), { kind: "approval", waitType: "callback", ongoing: true });
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline, viewIndex: 0, onScrub() {} }
    }).body);
    const marks = [...html.matchAll(/<button[^>]*class="(mark [^"]*)"/g)].map(([, cls]) => cls);
    assert.deepEqual(marks.map((cls) => / wait\b/.test(cls)), [false, true, true]);
    assert.match(source, /\.mark\.wait \.connector \{\s*background: color-mix\(in srgb, currentColor 35%, transparent\);/);
    assert.doesNotMatch(source, /\.mark\.wait\.state-ongoing/, "an open wait runs the work's dash over its tint");
    const cap = source.match(/\n {2}\.cap \{([^}]*)\}/)[1];
    assert.match(cap, /box-shadow: inset 0 0 0 1px currentColor;/, "its caps stay filled, outlined squares");
  });

  it("runs thin marching sticks on an ongoing span and names that state", () => {
    const timeline = timelineWith(1);
    timeline.workflow.rowAt(0).ongoing = true;
    const html = stripComments(render(WorkflowTimeline, {
      props: { timeline, viewIndex: 0, onScrub() {} }
    }).body);
    assert.match(html, /class="mark [^"]*state-ongoing/);
    assert.match(html, /class="connector dash[\s"]/);
    assert.match(html, /aria-label="span 0 · 500ms · ongoing"/);
    const dash = source.match(/\n {2}\.state-ongoing \.dash::before \{([^}]*)\}/)[1];
    const [, stick, gapFrom, period] = dash.match(
      /repeating-linear-gradient\(\s*to right,\s*currentColor 0 (\d+)px,\s*transparent (\d+)px (\d+)px\s*\)/
    ).map(Number);
    assert.equal(stick, 1, "each stick is a 1px hairline");
    assert.equal(gapFrom, stick, "a hard stop, so the stick has no soft edge");
    assert.ok(period >= 4 && period <= 6, `a stick every ${period}px reads as a conveyor, not a barcode`);
    assert.match(dash, new RegExp(`inset: 0 -${period}px 0 0;`), "one period of overhang to scroll into");
    assert.match(dash, new RegExp(`animation: march var\\(--dash-cycle\\) steps\\(${period}\\) infinite;`), "a whole pixel a step");
    assert.match(source, new RegExp(`@keyframes march \\{\\s*to \\{ transform: translateX\\(-${period}px\\); \\}`), "one period per cycle");
    assert.match(source, /\.state-ongoing \.connector \{[^}]*background: transparent;/, "no box round the sticks");
    assert.match(source, /@media \(prefers-reduced-motion: reduce\) \{\s*\.state-ongoing \.dash::before \{\s*animation: none;/);
  });

  it("runs a wait still open at the newest frame on to now while live, and to the cursor in replay", () => {
    const requested = Math.floor(Date.now() / 1000) - 90;
    const wait = {
      key: "parent:1:span:ask",
      source: "span",
      kind: "approval",
      waitType: "callback",
      label: "ask_user",
      tone: "approval",
      startTs: requested,
      endTs: requested,
      startIndex: 2,
      endIndex: 2,
      ongoing: true,
      parentTurnNumber: 1,
      workflowId: "root",
      depth: 0
    };
    const timeline = {
      turns: [],
      workflow: {
        subagents: [],
        points: [[1, requested - 20], [2, requested]],
        bounds: { startTs: requested - 20, endTs: requested },
        gaps: [],
        ongoing: true,
        rowCount: 1,
        rowAt: (index) => (index === 0 ? wait : undefined)
      }
    };
    const markOf = (props) =>
      stripComments(render(WorkflowTimeline, { props: { timeline, viewIndex: 2, onScrub() {}, ...props } }).body)
        .match(/<button[^>]*class="mark [\s\S]*?<\/button>/)[0];

    const live = markOf({ live: true });
    assert.match(live, /class="connector dash[\s"]/, "a 90s wait marches instead of sitting as a dot");
    assert.doesNotMatch(live, /class="cap point[\s"]/);
    assert.match(live, /aria-label="ask_user · 1m 3\ds · ongoing"/);

    assert.match(markOf({ live: false }), /class="cap point[\s"]/, "replay at the newest frame has nowhere later to run to");
    timeline.workflow.points.push([3, requested + 30]);
    timeline.workflow.bounds.endTs = requested + 30;
    const replay = markOf({ live: false, viewIndex: 3 });
    assert.match(replay, /class="connector dash[\s"]/);
    assert.match(replay, /aria-label="ask_user · 30s · ongoing"/);
  });

  // Temporal Web UI's timeline: an 18px line in a 24px row, under 20px dots.
  it("draws square caps on Temporal's timeline proportions", () => {
    const row = Number(source.match(/\n {2}\.timeline-row \{[^}]*?\bheight: (\d+)px;/)?.[1]);
    assert.ok(row, "the row has a height");
    const mark = source.match(/\n {2}\.mark \{([^}]*)\}/)?.[1] ?? "";
    const lane = Number(mark.match(/\bheight: (\d+)px;/)?.[1]);
    assert.equal(lane, 2 * Math.floor((row * 0.75) / 2), "the lane is the even size at or under three quarters of the row");
    assert.match(mark, new RegExp(`\\btop: ${(row - lane) / 2}px;`), "the lane is centred in the row");
    assert.match(source, /\.connector \{[^}]*inset: 0;/, "the line, and the running dash in it, fill the lane");
    const cap = Number(mark.match(/--cap: (\d+)px;/)?.[1]);
    assert.equal(cap, lane + 2, "caps stand 1px proud of the line on each side");
    assert.equal(source.match(/--cap:/g)?.length, 1, "one cap size at every width, the glyph's included");
    const capRule = source.match(/\n {2}\.cap \{([^}]*)\}/)?.[1] ?? "";
    assert.match(capRule, /border-radius: var\(--radius-xs\);/);
    assert.doesNotMatch(source, /border-radius: (?:50%|999px)/, "no round marks");
    assert.match(capRule, /box-shadow: inset 0 0 0 1px currentColor;/, "a 1px state-coloured stroke");
    assert.doesNotMatch(capRule, /\bborder:/, "inset, which keeps the cap 22px");
    const fill = capRule.match(/\bbackground: ([^;]+);/)?.[1];
    const line = source.match(/\n {2}\.connector \{[^}]*\bbackground: ([^;]+);/)?.[1];
    assert.ok(fill && line && fill !== line, `the cap fill (${fill}) is not the line's (${line})`);
  });

  it("draws every stroke at the Latency pane's hairline, and only a focus ring heavier", () => {
    const latency = readFileSync(new URL("./LatencyWaterfall.svelte", import.meta.url), "utf8");
    const hairline = latency.match(/\n {2}\.bar \{[^}]*\bborder: (\d+px) solid transparent;/)?.[1];
    const ring = latency.match(/\n {2}\.turn-toggle:focus-visible \{\s*outline: (\d+px) solid var\(--focus-ring\);/)?.[1];
    assert.ok(hairline && ring, "Latency's bar outline and focus ring");
    const style = source.slice(source.indexOf("<style>"));
    const strokes = [...style.matchAll(/([^{}]+)\{([^{}]*)\}/g)].flatMap(([, before, body]) => {
      const selector = before.split("*/").at(-1);
      return [...body.matchAll(/^\s+(border(?!-radius)[a-z-]*|outline|box-shadow|stroke-width): ([^;]+);$/gm)]
        .filter(([, , value]) => value !== "0" && value !== "none")
        .map(([, property, value]) => ({
          rule: `${selector.trim()} { ${property} }`,
          width: property === "stroke-width" ? `${value}px` : value.match(/(\d+(?:\.\d+)?px) (?:solid|dashed|currentColor|color-mix|var)/)?.[1],
          focus: property === "outline" && selector.includes(":focus-visible")
        }));
    });
    const stick = source.match(/\n {2}\.state-ongoing \.dash::before \{[^}]*currentColor 0 (\d+(?:\.\d+)?px),/)?.[1];
    strokes.push({ rule: ".state-ongoing .dash::before { stick }", width: stick, focus: false });
    for (const rule of [".cap { box-shadow }", ".gap-toggle svg { stroke-width }", ".playhead { box-shadow }", ".timeline-row:focus-visible { outline }"]) {
      assert.ok(strokes.some((stroke) => stroke.rule === rule), `${rule} is read`);
    }
    for (const { rule, width, focus } of strokes) {
      assert.equal(width, focus ? ring : hairline, rule);
    }
  });
});
