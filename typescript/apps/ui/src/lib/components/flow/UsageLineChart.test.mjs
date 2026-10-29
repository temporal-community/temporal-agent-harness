// ABOUTME: Asserts what the token chart draws from the per-call usage series. The slots and marks come
// from usageChart.ts, a plain module, so these tests call the real functions; the component is
// rendered through svelte/server where the drawn SVG is the claim (the playhead's position).
// The properties: an empty series makes no slots, each call keeps its own count beside the running
// total, a call with no usage is unknown and never a zero, calls sharing an instant merge, an
// out-of-order series still rises, the marker sits on the replay cursor and reads nothing before the
// first call, and 100k-frame runs are bounded to a few hundred bars. Past one model the run's total
// is its own heavier line, waits sit in rows under the baseline, and each legend entry hides its series.
// The tooltip's rows and the readout under the plot lead each figure with the same icon.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { buildUsageTimeline, usagePointAt } from "$lib/cost/pricing.ts";
import { formatDuration, formatElapsedDuration } from "$lib/state/replayLog.ts";
import { Chart } from "@tanstack/charts/svelte";
import { buildStepTimeline, findWorkflowIdleGaps } from "$lib/state/stepTimeline.ts";
import { buildWorkflowScale, gapBreakX, projectTime } from "$lib/state/workflowTimelineScale.ts";
import {
  callCategories,
  categorySum,
  categoryTotals,
  drawsTotal,
  durationTick,
  IDLE_GAP_WIDTH,
  logTokens,
  MAX_SLOTS,
  mergeToolTicks,
  MODEL_LINE_STYLES,
  MODEL_WIDTH,
  modelKey,
  playheadAxisX,
  presentCategories,
  segmentSpan,
  slotBudget,
  slotTipFields,
  TOKEN_CATEGORIES,
  TOKEN_CATEGORY_STYLES,
  TOTAL_LINE_STYLE,
  TOTAL_WIDTH,
  usageActivity,
  usageChart,
  usageGaps,
  usageSlots,
  usageTipBelow,
  unreportedCategories,
  waitedSeconds
} from "./usageChart.ts";
import TipCard from "$lib/components/primitives/TipCard.svelte";
import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import UsageLineChart from "./UsageLineChart.svelte";

const ended = (timestamp, usage, model = "gpt-5.4-mini") => ({
  event: "model_interaction_ended",
  data: { type: "model_interaction_ended", timestamp, agent_id: "agent", model, usage }
});
const started = (timestamp, model = "gpt-5.4-mini") => ({
  event: "model_interaction_started",
  data: { type: "model_interaction_started", timestamp, agent_id: "agent", model }
});
const other = (timestamp) => ({ event: "reply_delta", data: { type: "reply_delta", timestamp, delta: "." } });
const spent = (total) => ({ input_tokens: total, output_tokens: 0, total_tokens: total });
const slotsOf = (frames, budget = 400) => usageSlots(buildUsageTimeline(frames), budget);

describe("the token chart's slots", () => {
  it("makes nothing of a run with no model calls", () => {
    const layout = slotsOf([other(100), other(110)]);
    assert.deepEqual(layout, { slots: [], lines: [], span: 0, downsampled: false });
    assert.equal(usagePointAt([], 5), undefined);
  });

  it("keeps each call's own tokens beside the running total", () => {
    const { slots, span } = slotsOf([ended(100, spent(500)), other(105), ended(110, spent(200)), ended(130, spent(50))]);
    assert.equal(span, 30);
    assert.deepEqual(slots.map((slot) => slot.tokens), [500, 200, 50], "per call");
    assert.deepEqual(slots.map((slot) => slot.running), [500, 700, 750], "running total");
    assert.deepEqual(slots.map((slot) => Math.round(slot.x)), [0, 10, 30], "each at its call's moment, to the slot");
    assert.deepEqual(slots.map((slot) => slot.index), [1, 3, 4], "1-based frame positions");
  });

  it("draws a call with no usage as unknown, not as a zero", () => {
    const { slots } = slotsOf([ended(100, spent(500)), ended(110, null, "relay"), ended(120, spent(100))]);
    assert.equal(slots.length, 3, "the silent call keeps its own slot");
    assert.equal(slots[1].unknown, 1);
    assert.equal(slots[1].tokens, 0, "it has no bar height of its own");
    assert.equal(slots[1].running, 500, "and adds nothing to the measured sum");
    assert.deepEqual(slots.map((slot) => slot.unknownSoFar), [0, 1, 1], "so everything after is a lower bound");
  });

  it("merges calls that share an instant into one slot", () => {
    const { slots } = slotsOf([ended(100, spent(10)), ended(100, spent(30), "other"), ended(110, spent(5))]);
    assert.equal(slots.length, 2);
    assert.equal(slots[0].calls, 2);
    assert.equal(slots[0].tokens, 40);
    assert.deepEqual(slots[0].models, ["gpt-5.4-mini", "other"]);
  });

  it("sorts calls from out-of-order agents so the running total never falls", () => {
    const { slots } = slotsOf([ended(130, spent(900)), ended(100, spent(10)), ended(115, spent(400))]);
    assert.deepEqual(slots.map((slot) => Math.round(slot.x)), [0, 15, 30]);
    assert.deepEqual(slots.map((slot) => slot.running), [10, 410, 1310]);
  });

  it("never puts two slots closer than one stretch of time", () => {
    const { slots, span } = slotsOf([ended(100, spent(5)), ended(100.2, spent(5)), ended(160, spent(5))], 100);
    const stretch = span / 100;
    for (let i = 1; i < slots.length; i += 1) {
      assert.ok(slots[i].x - slots[i - 1].x >= stretch - 1e-9, "bars are sized from the tightest gap");
    }
  });

  /* frames100k and dense50k each hold 12,500 calls. Bars past one per few pixels are
     marks nobody can see, and every one costs layout on each scrub. */
  it("bounds a long run to the plot's slot budget and keeps every token", () => {
    const frames = [];
    let expected = 0;
    for (let n = 0; n < 12_500; n += 1) {
      frames.push(other(n * 0.2), ended(n * 0.2 + 0.1, n % 997 === 0 ? null : spent(100 + (n % 7))));
      if (n % 997 !== 0) expected += 100 + (n % 7);
    }
    for (const width of [0, 120, 420, 1440, 10_000]) {
      const budget = slotBudget(width);
      assert.ok(budget >= 1 && budget <= MAX_SLOTS, `width ${width}: budget ${budget}`);
      const { slots } = slotsOf(frames, budget);
      assert.ok(slots.length <= budget, `width ${width}: ${slots.length} slots for a budget of ${budget}`);
      assert.equal(slots.reduce((sum, slot) => sum + slot.tokens, 0), expected, "no token is dropped by merging");
      assert.equal(slots.at(-1).running, expected);
      assert.equal(slots.reduce((sum, slot) => sum + slot.unknown, 0), 13, "nor is an unknown call");
      assert.equal(slotsOf(frames, budget).downsampled, true, "and the layout says its bars are stretches of time");
    }
  });
});

describe("token categories", () => {
  const totals = (fields) => ({ input: 0, output: 0, thought: 0, cached: 0, toolUse: 0, total: 0, ...fields });

  it("splits a call into disjoint parts that add up to its total, whichever way the provider overlaps them", () => {
    /* Gemini: thought and tool-use beside output, all in its total. */
    const gemini = callCategories(totals({ input: 2041, output: 74, thought: 94, cached: 41, toolUse: 12, total: 2221 }));
    assert.deepEqual(gemini, { uncached: 2000, cached: 41, output: 74, thought: 94, toolUse: 12, decisions: 0 });
    assert.equal(categorySum(gemini), 2221);
    /* OpenAI and pydantic-ai: thought inside output, total = input + output. */
    const openai = callCategories(totals({ input: 500, output: 300, thought: 100, cached: 200, total: 800 }));
    assert.deepEqual(openai, { uncached: 300, cached: 200, output: 200, thought: 100, toolUse: 0, decisions: 0 });
    assert.equal(categorySum(openai), 800);
    /* A decision model's call is all decisions. */
    assert.deepEqual(callCategories(totals({ input: 100, output: 10, total: 110 }), true), { uncached: 0, cached: 0, output: 0, thought: 0, toolUse: 0, decisions: 110 });
  });

  it("never draws a negative part", () => {
    const odd = callCategories(totals({ input: 100, cached: 400, output: 50, thought: 80, total: 150 }));
    assert.equal(odd.uncached, 0, "cached past input leaves no uncached input, not a negative one");
    assert.equal(odd.output, 0, "thought past output leaves no output");
    for (const value of [Number.NaN, -5, Number.POSITIVE_INFINITY]) {
      const parts = callCategories(totals({ input: value, output: value, thought: value, cached: value, toolUse: value, total: value }));
      for (const [category, count] of Object.entries(parts)) assert.equal(count, 0, `${category} from ${value}`);
    }
  });

  it("sums each slot's parts, numbers its call, and stacks the parts bottom to top in order", () => {
    const decision = {
      event: "auto_approval_evaluation_ended",
      data: { type: "auto_approval_evaluation_ended", timestamp: 120, evaluation_id: "e", details: { model: "jev", usage: { input_tokens: 90, output_tokens: 10, total_tokens: 100 } } }
    };
    const frames = [
      ended(100, { input_tokens: 2041, output_tokens: 74, thought_tokens: 94, cached_tokens: 41, tool_use_tokens: 12, total_tokens: 2221 }, "gemini"),
      ended(110, { input_tokens: 500, output_tokens: 300, thought_tokens: 100, cached_tokens: 200, tool_use_tokens: null, total_tokens: 800 }),
      decision
    ];
    const points = buildUsageTimeline(frames);
    const { slots } = usageSlots(points, 400);
    assert.deepEqual(slots.map((slot) => slot.call), [1, 2, 3]);
    for (const slot of slots) assert.equal(categorySum(slot.parts), slot.tokens, "a bar's parts are its whole height");
    assert.deepEqual(categoryTotals(points), { uncached: 2300, cached: 241, output: 274, thought: 194, toolUse: 12, decisions: 100 });
    assert.deepEqual(categoryTotals(points, 2), { ...categoryTotals(points), decisions: 0 }, "up to the cursor");
    assert.deepEqual(presentCategories(points), TOKEN_CATEGORIES);
    assert.deepEqual([...unreportedCategories(points)], [["toolUse", ["gpt-5.4-mini"]]], "the decision call's missing fields do not count");

    const first = slots[0];
    let top = 0;
    for (const category of TOKEN_CATEGORIES.slice(0, 5)) {
      const [bottom, up] = segmentSpan(first, category);
      assert.ok(Math.abs(bottom - top) < 1e-9, `${category} sits on the part under it`);
      top = up;
    }
    assert.ok(Math.abs(top - logTokens(first.tokens)) < 1e-9, "and the stack is the bar's log height");
    assert.equal(segmentSpan(first, "decisions"), null, "a part the slot spent none of draws nothing");

    const html = render(UsageLineChart, { props: { points, viewIndex: 3 } }).body;
    const attr = (tag, name) => Number(tag.match(new RegExp(`${name}="([\\d.-]+)"`))[1]);
    const ys = TOKEN_CATEGORIES.slice(0, 5).map((category) => attr(html.match(new RegExp(`<rect data-ts-key="part-${category}:[^"]*:${category}-1"[^>]*>`))[0], "y"));
    for (let k = 1; k < ys.length; k += 1) assert.ok(ys[k] < ys[k - 1], `${TOKEN_CATEGORIES[k]} is drawn above ${TOKEN_CATEGORIES[k - 1]}`);
    assert.match(html, /<rect data-ts-key="part-decisions:[^"]*:decisions-3" fill="var\(--text-3\)"/);
    assert.match(html, /Each bar stacks input \(uncached\), cached, output, thought, tool-use, decisions, bottom to top/, "the parts are in the chart's description");
    assert.ok(html.match(/<rect data-ts-key="calls:[^>]*>/g).every((bar) => /fill="transparent"/.test(bar)), "the focus target is clear under its parts");
    assert.match(html, /call 3/, "the cursor line numbers the call");
  });
});

describe("the replay marker", () => {
  const frames = [ended(100, spent(500)), other(105), ended(110, null, "relay"), other(115), ended(120, spent(100))];
  const points = buildUsageTimeline(frames);

  it("follows the replay cursor and reads nothing before the first call", () => {
    assert.equal(usagePointAt(points, 0), undefined, "before the first call there is no reading");
    assert.equal(usagePointAt(points, 1).timestamp, 100);
    assert.equal(usagePointAt(points, 2).timestamp, 100, "between calls it holds the last one reached");
    assert.equal(usagePointAt(points, 3).timestamp, 110);
    assert.equal(usagePointAt(points, 99).timestamp, 120, "past the end it rests on the last call");
    assert.deepEqual(
      [1, 3, 5].map((index) => usagePointAt(points, index).usage.tokens.total),
      [500, 500, 600],
      "the at-cursor total is the cumulative sum through that frame"
    );
  });

  it("uses frame order when concurrent calls arrive out of timestamp order", () => {
    const points = buildUsageTimeline([
      ended(130, spent(900), "late-clock"),
      ended(100, spent(10), "early-clock"),
      ended(115, spent(400), "middle-clock")
    ]);
    assert.equal(usagePointAt(points, 2).timestamp, 100);
    assert.equal(usagePointAt(points, 2).usage.tokens.total, 910);

    const html = render(UsageLineChart, { props: { points, viewIndex: 2 } }).body;
    const marker = Number(html.match(/<line data-ts-key="rule-x[^>]*x1="([\d.]+)"/)[1]);
    const firstBar = html.match(/data-ts-key="calls:[^"]*:calls-2"[^>]*x="([\d.]+)"[^>]*width="([\d.]+)"/);
    assert.ok(Math.abs(marker - (Number(firstBar[1]) + Number(firstBar[2]) / 2)) < 0.01);
  });

  /* The drawn claim: the playhead rule sits at the x the cursor's slot was drawn at. */
  it("draws the playhead on the cursor's call", () => {
    const playheadAt = (viewIndex) => {
      const html = render(UsageLineChart, { props: { points: buildUsageTimeline(frames), viewIndex } }).body;
      const rule = html.match(/<line data-ts-key="rule-x[^>]*x1="([\d.]+)"/);
      assert.ok(rule, "a playhead is drawn");
      return Number(rule[1]);
    };
    const barX = (html, key) => {
      const bar = html.match(new RegExp(`data-ts-key="(?:calls|unknown):[^"]*:${key}"[^>]*x="([\\d.]+)"[^>]*width="([\\d.]+)"`));
      return Number(bar[1]) + Number(bar[2]) / 2;
    };
    const at = (viewIndex) => render(UsageLineChart, { props: { points: buildUsageTimeline(frames), viewIndex } }).body;
    const html = at(3);
    assert.equal(playheadAt(3), barX(html, "unknown-3"), "at the silent call's column");
    assert.doesNotMatch(html, /calls-5/, "the call after the cursor is not drawn");
    assert.equal(playheadAt(5), barX(at(5), "calls-5"), "at the last call's bar");
    const origin = Number(html.match(/data-ts-key="x-tick-rule:number:0"[^>]*x1="([\d.]+)"/)[1]);
    assert.equal(playheadAt(0), origin, "at the run's start before the first call");
    const before = at(0);
    assert.doesNotMatch(before, /<circle/, "and with no reading there is no dot on the running line");
    assert.doesNotMatch(before, /data-ts-key="(?:calls|unknown|model-|total):/, "and no series has happened yet");
  });

  /* The series the chart draws, not each mark: a cursor before the first call is an
     empty chart on the run's axis, and a cursor in the middle keeps only the calls
     it has reached, including when two calls share an instant or a bucket. */
  it("derives empty series before the first call and only the calls reached after it", () => {
    const frames = [
      ended(100, spent(500)),
      ended(100, spent(30), "other"),
      ended(110, spent(200)),
      ended(130, spent(50), "other")
    ];
    const points = buildUsageTimeline(frames);
    const full = usageSlots(points, 400);

    const before = usageSlots(points, 400, 0);
    assert.deepEqual(before.slots, []);
    assert.deepEqual(before.lines, []);
    assert.equal(before.span, full.span, "the axis span stays the whole run");

    const firstOnly = usageSlots(points, 400, 1);
    assert.equal(firstOnly.slots.length, 1);
    assert.equal(firstOnly.slots[0].calls, 1, "the other call at that instant has not happened");
    assert.deepEqual(firstOnly.slots.map((slot) => slot.tokens), [500]);
    assert.deepEqual(firstOnly.slots.map((slot) => slot.running), [500], "the total steps only as a call happens");
    assert.equal(firstOnly.lines.length, 1);
    assert.equal(firstOnly.slots[0].x, full.slots[0].x, "the bar stays where the full run draws it");

    const mid = usageSlots(points, 400, 3);
    assert.deepEqual(mid.slots.map((slot) => slot.index), [2, 3]);
    assert.deepEqual(mid.slots.map((slot) => slot.tokens), [530, 200]);
    assert.deepEqual(mid.slots.map((slot) => slot.running), [530, 730]);
    assert.deepEqual(mid.slots.map((slot) => slot.x), full.slots.slice(0, 2).map((slot) => slot.x));
    assert.equal(mid.lines.length, 2, "both models reached, the later call's model included");
    assert.equal(mid.span, full.span);
    assert.deepEqual(usageSlots(points, 400, points.at(-1).index), full, "at the last call every series is the whole run");

    const crowded = Array.from({ length: 20 }, (_, n) => ended(100 + n * 0.01, spent(10)));
    const crowdedPoints = buildUsageTimeline(crowded);
    const oneBucket = usageSlots(crowdedPoints, 1, 5);
    assert.equal(oneBucket.slots.length, 1);
    assert.equal(oneBucket.slots[0].calls, 5, "a shared bucket counts only the calls the cursor has reached");
    assert.equal(oneBucket.slots[0].tokens, 50);
    assert.equal(oneBucket.slots[0].running, 50);
    assert.equal(oneBucket.span, usageSlots(crowdedPoints, 1).span);
    assert.equal(oneBucket.slots[0].x, usageSlots(crowdedPoints, 1).slots[0].x);

    const ticks = (html) =>
      [...html.matchAll(/data-ts-key="x-tick-rule:number:[^"]*"[^>]*x1="([\d.]+)"/g)].map((match) => match[1]);
    const atStart = render(UsageLineChart, { props: { points, viewIndex: 0 } }).body;
    const atEnd = render(UsageLineChart, { props: { points, viewIndex: points.at(-1).index } }).body;
    assert.deepEqual(ticks(atStart), ticks(atEnd), "the time axis does not move as the cursor does");
    assert.equal(
      atStart.match(/data-ts-key="running-grid:/g)?.length,
      atEnd.match(/data-ts-key="running-grid:/g)?.length,
      "nor does the token grid"
    );
    const midHtml = render(UsageLineChart, { props: { points, viewIndex: 3 } }).body;
    const total = midHtml.match(/<path data-ts-key="total:[^"]*"[^>]*d="([^"]*)"/);
    assert.ok(total, "the total line is drawn once two models have happened");
    assert.match(total[1], /H[\d.]+V/, "and it steps up at a call instead of sloping across the gap");
    assert.doesNotMatch(midHtml, /calls-4/, "the call after the cursor is not a bar");
  });
});

describe("the call at the cursor", () => {
  it("shows the call's model, token parts, duration, and rate", () => {
    const frames = [
      started(100, "provider/model-with-a-very-long-identifier"),
      ended(102, { input_tokens: 2400, output_tokens: 90, cached_tokens: 25, total_tokens: 2490 }, "provider/model-with-a-very-long-identifier")
    ];
    const html = render(UsageLineChart, { props: { points: buildUsageTimeline(frames), viewIndex: 2 } }).body;
    assert.match(html, /data-tip="provider\/model-with-a-very-long-identifier"/);
    assert.match(html, /2\.4K in/);
    assert.match(html, /90 out/);
    assert.match(html, /25 cache/);
    assert.match(html, /2s/);
    assert.match(html, /1\.2K tok\/s/);
    assert.match(html, /class="cursor-call[^"]*" dir="ltr"/, "numeric fields keep their order in RTL");
    const source = readFileSync(new URL("./UsageLineChart.svelte", import.meta.url), "utf8");
    assert.match(source, /\.cursor-call \{[^}]*flex-wrap: wrap;/, "the readout wraps rather than clipping");
  });

  it("says when the call reported no usage", () => {
    const html = render(UsageLineChart, { props: { points: buildUsageTimeline([ended(100, null)]), viewIndex: 1 } }).body;
    assert.match(html, /Usage not reported/);
  });

  it("leads each figure with the tooltip's icon for it, and gives a call that took no time no rate", () => {
    const frames = [started(100), ended(100, { input_tokens: 3100, output_tokens: 269, cached_tokens: 0, total_tokens: 3369 })];
    const html = stripComments(render(UsageLineChart, { props: { points: buildUsageTimeline(frames), viewIndex: 2 } }).body);
    const readout = html.match(/<p class="cursor-call[\s\S]*?<\/p>/)?.[0] ?? "";
    for (const icon of ["cpu", "clock", "hash", "arrow-down-to-line", "arrow-up-from-line", "database-zap", "timer"]) {
      assert.match(readout, new RegExp(`class="reading[^"]*"[^>]*>\\s*<svg[^>]*lucide-${icon}\\b`), icon);
    }
    assert.doesNotMatch(readout, /tok\/s|lucide-gauge/, "no rate over zero seconds");
    assert.match(readout, /class="fade-end"[^>]*>gpt-5\.4-mini</, "the model fades rather than ending in an ellipsis");
  });
});

/* The Session Manager's tip pattern: an icon in place of each key, kept from assistive tech, and a
   value worded to name its own metric, so the row reads whole with no key and no bare icon. */
describe("the token chart's tooltip rows", () => {
  const names = { Model: /gpt-5\.4-mini/, Models: /gpt-5\.4-mini.*other/, Call: /^call \d/, Calls: /^\d+ calls · \d+–\d+$/, Running: /so far$/, At: /^\+\d/ };
  const every = { input_tokens: 900, output_tokens: 300, thought_tokens: 40, cached_tokens: 200, tool_use_tokens: 20, total_tokens: 1260 };

  for (const [label, frames] of [
    ["one call", [ended(100, every)]],
    ["merged calls", [ended(100, every), ended(100, every, "\u0646\u0645\u0648\u0630\u062c"), ended(100, every, "other")]]
  ]) {
    it(`renders an icon before every metric row, with the row named by its value (${label})`, () => {
      const { slots } = slotsOf(frames);
      const fields = slotTipFields(slots[0], TOKEN_CATEGORIES);
      const card = stripComments(render(TipCard, { props: { fields } }).body);
      const rows = [...card.matchAll(/<span class="tip-card-icon[^"]*" aria-hidden="true">(<svg[\s\S]*?<\/svg>)<\/span>\s*<span class="tip-card-value[^"]*" dir="auto">([^<]*)</g)];
      const shown = fields.filter((field) => field.value);
      assert.equal(rows.length, shown.length, "one icon per row that has a value");
      assert.doesNotMatch(card, /tip-card-key/, "no text key beside the icon");
      rows.forEach(([, svg, value], i) => {
        const { key } = shown[i];
        assert.match(svg, /class="lucide-icon lucide/, `${key} draws a Lucide icon`);
        const category = TOKEN_CATEGORIES.find((c) => TOKEN_CATEGORY_STYLES[c].label === key);
        const name = category ? new RegExp(`${TOKEN_CATEGORY_STYLES[category].label.replace(/[()]/g, "\\$&")}$`) : names[key];
        assert.match(value.replace(/[\u2068\u2069]/g, ""), name, `${key}'s value says what it is: "${value}"`);
      });
    });
  }
});

describe("the token chart's log axes", () => {
  const attr = (tag, name) => Number(tag.match(new RegExp(`${name}="([\\d.-]+)"`))[1]);
  const bars = (markup) => markup.match(/<rect data-ts-key="calls:[^>]*>/g).map((bar) => attr(bar, "height"));
  const frames = [ended(100, spent(10)), ended(110, null, "relay"), ended(120, spent(100)), ended(130, spent(1_000)), ended(140, spent(10_000))];
  const html = render(UsageLineChart, { props: { points: buildUsageTimeline(frames), viewIndex: 5 } }).body;

  /* The side labels took the plot's width; the tooltip and the figures carry the numbers. */
  it("draws no y tick labels on either side, and a gridline per decade", () => {
    assert.doesNotMatch(html, /data-ts-key="(y|running)-tick-label/);
    assert.match(html, /data-ts-key="x-tick-label/, "the time axis stays");
    assert.equal(html.match(/data-ts-key="running-grid:/g)?.length, 4, "10, 100, 1K and 10K");
    assert.doesNotMatch(html, /data-ts-key="y-grid:/);
  });

  /* On a linear axis a 10-token call is a thousandth of a 10K one and draws as nothing. */
  it("puts a call's height on a log scale", () => {
    const [h10, h100, h1k, h10k] = bars(html);
    assert.ok(
      Math.abs(h100 - h10 - (h1k - h100)) < 1.5 && Math.abs(h1k - h100 - (h10k - h1k)) < 1.5,
      `each tenfold is one step (${[h10, h100, h1k, h10k]})`
    );
    assert.ok(h10 > h10k / 5, "the smallest call keeps a visible height");
    const silent = html.match(/<rect data-ts-key="unknown:[^>]*>/)[0];
    assert.ok(attr(silent, "height") <= 5, "a silent call stays a stub");
  });

  it("maps zero, negative and non-finite counts to the floor", () => {
    for (const value of [0, -5, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      assert.equal(logTokens(value), 0, String(value));
    }
    assert.ok(Math.abs(logTokens(999) - 3) < 1e-12);
    const points = buildUsageTimeline([ended(100, spent(0), "zero"), ended(110, spent(500)), ended(120, spent(0), "zero")]);
    const zeros = render(UsageLineChart, { props: { points, viewIndex: 3 } }).body;
    assert.match(zeros, /data-ts-key="model-1:/, "the all-zero model still gets its line");
    assert.doesNotMatch(zeros, /NaN|Infinity/, "and nothing in the SVG is NaN");
  });
});

describe("one line per model", () => {
  const models = ["big", "mid", "small", "tiny", "least", "last"];
  const frames = models.flatMap((model, k) => [ended(100 + k, spent(10 ** (6 - k)), model), ended(200 + k, spent(10 ** (6 - k)), model)]);

  it("draws each model's running total, largest first, and sums those past the styled ones", () => {
    const { lines } = slotsOf(frames);
    assert.equal(MODEL_LINE_STYLES.length, 4);
    assert.deepEqual(lines.map((line) => line.label), ["big", "mid", "small", "tiny", "2 other models"]);
    assert.deepEqual(lines.map((line) => line.line), [0, 1, 2, 3, null]);
    assert.equal(lines[0].running.at(-1), 2_000_000);
    assert.equal(lines[4].running.at(-1), 2 * (100 + 10), "the rest are one summed line");
    const html = render(UsageLineChart, { props: { points: buildUsageTimeline(frames), viewIndex: frames.length } }).body;
    for (let k = 0; k < 5; k += 1) assert.match(html, new RegExp(`data-ts-key="model-${k}:`), `line ${k}`);
    assert.match(html, /data-ts-key="total:/, "and the run's total over them");
  });

  it("draws one line for one model and fades a model's line once its total is a lower bound", () => {
    assert.equal(slotsOf([ended(100, spent(5)), ended(110, spent(5))]).lines.length, 1);
    const silent = [ended(100, spent(5), "a"), ended(110, null, "a"), ended(120, spent(5), "b")];
    assert.equal(slotsOf(silent).lines.find((line) => line.label === "a").unknownFrom, 1);
    const html = render(UsageLineChart, { props: { points: buildUsageTimeline(silent), viewIndex: 3 } }).body;
    assert.match(html, /data-ts-key="model-0-bound:/);
  });
});

describe("workflow activity on the token chart", () => {
  const span = (kind, label, startTs, endTs) => ({ kind, label, startTs, endTs, durationSeconds: endTs - startTs });
  const timeline = {
    turns: [{
      spans: [span("tool", "read_file", 105, 110), span("approval", "approval · shell", 112, 118)],
      subagentTurns: [{ spans: [span("approval", "callback · ask_user", 120, 125)] }]
    }],
    workflow: { gaps: [] }
  };

  it("reuses the latency projection for tool starts and waits", () => {
    assert.deepEqual(usageActivity(timeline, 100, 30), [
      { kind: "tool", x1: 5, x2: 10, label: "read_file", durationSeconds: 5, lane: 0 },
      { kind: "wait", x1: 12, x2: 18, label: "approval · shell", durationSeconds: 6, lane: 0 },
      { kind: "wait", x1: 20, x2: 25, label: "callback · ask_user", durationSeconds: 5, lane: 0 }
    ]);
  });

  /* The regression: an approval inside an hour-long callback drew as one brighter
     strip on the baseline, which read as the chart's floor rather than two waits. */
  it("gives overlapping waits their own rows under the baseline and counts their time once", () => {
    const nested = {
      turns: [{
        spans: [span("approval", "callback · ask_user", 100, 3700), span("approval", "approval · deploy", 700, 2500), span("tool", "read_file", 900, 904)],
        subagentTurns: []
      }],
      workflow: { gaps: [] }
    };
    const activity = usageActivity(nested, 100, 3600);
    assert.deepEqual(activity.filter((item) => item.kind === "wait").map((item) => item.lane), [0, 1]);
    assert.equal(waitedSeconds(activity), 3600, "nested waits are not double counted");

    const points = buildUsageTimeline([ended(100, spent(30_000)), ended(3700, spent(500))]);
    const html = render(UsageLineChart, { props: { points, timeline: nested, viewIndex: 2 } }).body;
    const attr = (tag, name) => Number(tag.match(new RegExp(`${name}="([\\d.-]+)"`))[1]);
    const waits = html.match(/<rect data-ts-key="wait:[^>]*>/g);
    const bar = html.match(/<rect data-ts-key="calls:[^>]*>/)[0];
    const floor = attr(bar, "y") + attr(bar, "height");
    assert.equal(waits.length, 2);
    for (const wait of waits) assert.ok(attr(wait, "y") >= floor, "every wait row sits under the bars");
    assert.notEqual(attr(waits[0], "y"), attr(waits[1], "y"), "and the two waits are on different rows");
  });

  it("draws activity behind the token series and names it in the legend", () => {
    const points = buildUsageTimeline([ended(100, spent(10)), ended(130, spent(20))]);
    const html = render(UsageLineChart, { props: { points, timeline, viewIndex: 2 } }).body;
    assert.ok(html.indexOf('data-ts-key="wait:') < html.indexOf('data-ts-key="calls:'), "wait bars are behind calls");
    assert.ok(html.indexOf('data-ts-key="tool:') < html.indexOf('data-ts-key="calls:'), "tool ticks are behind calls");
    const source = readFileSync(new URL("./UsageLineChart.svelte", import.meta.url), "utf8");
    assert.match(source, /"Tools"/);
    assert.match(source, /Approval \/ callback waits\$\{/);
  });

  /* run100k drew ~33,000 tool rects, one per call, two pixels wide each, on a plot of a few
     hundred columns. */
  it("draws one tool tick per pixel column and one bar per slot, and keeps every token", () => {
    const calls = 100_000;
    const frames = Array.from({ length: calls }, (_, n) => ended(100 + n * 0.2, { input_tokens: 90 + (n % 7), output_tokens: 10, cached_tokens: n % 3, total_tokens: 100 + (n % 7) }));
    const points = buildUsageTimeline(frames);
    const layout = usageSlots(points, slotBudget(800));
    assert.ok(layout.slots.length <= 800, `${layout.slots.length} bars`);
    const summed = Object.fromEntries(TOKEN_CATEGORIES.map((category) => [category, layout.slots.reduce((sum, slot) => sum + slot.parts[category], 0)]));
    assert.deepEqual(summed, categoryTotals(points), "the bars' stacks sum to the run's totals");
    assert.equal(layout.slots.reduce((sum, slot) => sum + slot.calls, 0), calls);
    assert.deepEqual([layout.slots[0].first, layout.slots[1].first, layout.slots.at(-1).call], [1, layout.slots[0].call + 1, calls], "each bar names its calls");

    const tools = Array.from({ length: 33_000 }, (_, n) => ({ kind: "tool", x1: n * 0.6, x2: n * 0.6 + 0.3, label: `tool_${n}`, durationSeconds: 0.3, lane: 0 }));
    const definition = usageChart({ layout, cursor: undefined, activity: tools, plotHeight: 240, reducedMotion: true });
    const html = render(Chart, { props: { definition, height: 240, width: 800, ariaLabel: "Tokens" } }).body;
    const ticks = html.match(/<rect data-ts-key="tool:/g).length;
    assert.ok(ticks <= 400, `${ticks} tool ticks on 800px`);
    assert.ok(html.match(/<rect data-ts-key="calls:/g).length <= 800);

    const merged = mergeToolTicks(tools.map((tool) => ({ ...tool, axisX1: tool.x1 / 25 })));
    assert.equal(merged.reduce((sum, tick) => sum + (tick.count ?? 1), 0), tools.length, "every tool is in a tick");
    assert.deepEqual([merged[0].label, merged[0].count, merged[0].last], ["tool_0", 84, { label: "tool_83", x1: 83 * 0.6 }]);
    assert.equal(mergeToolTicks([tools[0], { ...tools[1], x1: 50 }].map((tool) => ({ ...tool, axisX1: tool.x1 })))[0].count, undefined, "a tick alone in its column is one call");
  });

  it("drops activity below the same narrow threshold as the left axis", () => {
    const source = readFileSync(new URL("./usageChart.ts", import.meta.url), "utf8");
    assert.match(source, /shownActivity = width < LEFT_AXIS_MIN\s*\? \[\]/);
  });
});

describe("the total line and the legend switches", () => {
  const frames = ["big", "mid", "small"].flatMap((model, k) => [ended(100 + k, spent(10 ** (5 - k)), model), ended(200 + k, spent(10 ** (5 - k)), model)]);
  const points = buildUsageTimeline(frames);
  const layout = usageSlots(points, 400);
  const tool = { kind: "tool", x1: 50, x2: 60, label: "read_file", durationSeconds: 10, lane: 0 };
  const wait = { kind: "wait", x1: 10, x2: 90, label: "approval · deploy", durationSeconds: 80, lane: 0 };
  const drawn = (hidden) => {
    const definition = usageChart({ layout, cursor: { x: layout.slots.at(-1).x }, activity: [tool, wait], plotHeight: 240, reducedMotion: true, hidden: new Set(hidden) });
    const html = render(Chart, { props: { definition, height: 240, width: 600, ariaLabel: "Tokens" } }).body;
    return { html, keys: new Set([...html.matchAll(/data-ts-key="([\w-]+):/g)].map((match) => match[1])) };
  };

  it("draws the total under the model lines, heavier and neutral", () => {
    const { html } = drawn([]);
    const path = (key) => html.match(new RegExp(`<path data-ts-key="${key}:[^>]*>`))?.[0] ?? assert.fail(`no ${key} line`);
    const total = path("total");
    assert.match(total, new RegExp(`stroke="${TOTAL_LINE_STYLE.color.replace(/[()]/g, "\\$&")}"`));
    assert.match(total, new RegExp(`stroke-width="${TOTAL_WIDTH}"`));
    for (const k of [0, 1, 2]) {
      const model = path(`model-${k}`);
      assert.match(model, new RegExp(`stroke-width="${MODEL_WIDTH}"`), `model ${k} is the lighter weight`);
      assert.doesNotMatch(model, new RegExp(`stroke="${TOTAL_LINE_STYLE.color.replace(/[()]/g, "\\$&")}"`), `model ${k} is not the total's color`);
      assert.ok(html.indexOf(total) < html.indexOf(model), `model ${k} stays visible on the total`);
    }
    assert.ok(TOTAL_WIDTH >= 2 * MODEL_WIDTH);
    assert.match(html, /data-ts-key="total-fill:/, "with a fill under it");
    assert.equal(drawsTotal(usageSlots(buildUsageTimeline([ended(100, spent(5)), ended(110, spent(5))]), 400)), false, "one model's line is already the total");
  });

  it("hides each series on its own and leaves the others drawn", () => {
    const all = ["total", "total-fill", "model-0", "model-1", "model-2", "tool", "wait", "calls"];
    const keys = drawn([]).keys;
    for (const key of all) assert.ok(keys.has(key), `${key} drawn`);
    const cases = [
      ["total", ["total", "total-fill"]],
      [modelKey(layout.lines[1]), ["model-1"]],
      ["tools", ["tool"]],
      ["waits", ["wait"]]
    ];
    for (const [hide, gone] of cases) {
      const { keys } = drawn([hide]);
      for (const key of gone) assert.ok(!keys.has(key), `${hide} hides ${key}`);
      for (const key of all) if (!gone.includes(key)) assert.ok(keys.has(key), `${hide} leaves ${key}`);
    }
    const { html } = drawn(["calls"]);
    assert.ok(html.match(/<rect data-ts-key="calls:[^>]*>/g).every((bar) => /fill="transparent"/.test(bar)), "hidden calls keep a clear focus target");
    const none = drawn(["calls", "total", ...layout.lines.map(modelKey), "tools", "waits"]).keys;
    for (const key of ["total", "model-0", "model-1", "model-2", "tool", "wait"]) assert.ok(!none.has(key), `all off hides ${key}`);
    assert.ok(none.has("x-tick-label") && none.has("calls"), "the time axis and the focus targets stay, so the plot is not a blank box");
  });

  it("renders every legend entry as a pressed switch and no scale heading", () => {
    const html = render(UsageLineChart, { props: { points, viewIndex: frames.length } }).body;
    const names = [...html.matchAll(/<button type="button" class="entry[^"]*" aria-pressed="true"[^>]*>.*?<span class="line-name[^>]*>([^<]*)<\/span><\/button>/g)].map((match) => match[1]);
    assert.deepEqual(names, ["Each call", "Total", "big", "mid", "small"]);
    assert.doesNotMatch(html, /class="legend"[^>]*aria-hidden/, "the switches are not hidden from assistive tech");
    /* The scale is the time axis and the decade grid. A heading for it was a row of dead space. */
    assert.doesNotMatch(html, /Over time/);
    assert.doesNotMatch(html, /<h3[\s>]/);
    const source = readFileSync(new URL("./UsageLineChart.svelte", import.meta.url), "utf8");
    assert.doesNotMatch(source, /Over time · log/);
  });
});

/* The reported run: two calls in the first second and a half, nothing for 15s, two
   more at the end. The chart kept the 15s at full width while the timeline folded it,
   because the two views picked gaps by different rules. */
describe("idle-gap collapse", () => {
  const at = (event, timestamp, data = {}) => ({
    event,
    data: { type: event, agent_id: "agent", turn_number: 1, timestamp, model: "gpt-5.1", ...data }
  });
  const call = (start, end) => [at("model_interaction_started", start), at("model_interaction_ended", end, { usage: spent(300) })];
  const frames = [
    at("turn_started", 0),
    ...call(0, 1),
    ...call(1, 1.5),
    at("reply_delta", 10, { delta: "." }),
    ...call(16.5, 17),
    ...call(17.5, 18)
  ];
  const timeline = buildStepTimeline(frames);
  const points = buildUsageTimeline(frames);
  const origin = points[0].timestamp;
  const barX = (html, index) => {
    const bar = html.match(new RegExp(`data-ts-key="calls:[^"]*:calls-${index}"[^>]*x="([\\d.]+)"[^>]*width="([\\d.]+)"`));
    assert.ok(bar, `bar for frame ${index}`);
    return Number(bar[1]) + Number(bar[2]) / 2;
  };

  it("folds the gaps the timeline folds, and the timeline's are unchanged", () => {
    assert.deepEqual(timeline.workflow.gaps, [{ key: "idle:1.5:16.5", startTs: 1.5, endTs: 16.5 }]);
    const rows = Array.from({ length: timeline.workflow.rowCount }, (_, index) => timeline.workflow.rowAt(index));
    assert.deepEqual(timeline.workflow.gaps, findWorkflowIdleGaps(rows, timeline.workflow.bounds), "one algorithm");
    assert.deepEqual(usageGaps(timeline, origin), [{ key: "idle:1.5:16.5", startTs: 0.5, endTs: 15.5 }], "seconds from the first call");
    assert.deepEqual(usageGaps(timeline, Infinity), [], "no calls, nothing to fold");
  });

  it("draws the break between the calls and keeps every bar in time order", () => {
    const html = render(UsageLineChart, { props: { points, timeline, viewIndex: frames.length } }).body;
    assert.match(html, /data-ts-key="idle-break-idle:1\.5:16\.5/, "the timeline's ≀ on the fold");
    const xs = [3, 5, 8, 10].map((index) => barX(html, index));
    assert.deepEqual(xs, xs.toSorted((a, b) => a - b), "left to right in time");
    /* 15 of the 17 seconds are folded into a few px, so the calls either side keep the plot. */
    assert.ok(xs[2] - xs[1] < (xs[1] - xs[0]) * 4, `${xs.join(", ")}: the fold is narrow`);
  });

  it("pins a playhead inside the fold to the break", () => {
    const segments = buildWorkflowScale({ startTs: 0, endTs: 17 }, usageGaps(timeline, origin), 400, IDLE_GAP_WIDTH);
    const gap = segments.find((segment) => segment.collapsed);
    assert.equal(playheadAxisX(segments, 0.5, 9), gapBreakX(gap), "inside the gap");
    assert.equal(playheadAxisX(segments, 0.5, 0.5), projectTime(segments, 0.5), "on its call");
    assert.equal(playheadAxisX(segments, 0.5), projectTime(segments, 0.5), "at the live edge");

    const reply = frames.findIndex((frame) => frame.event === "reply_delta") + 1;
    const html = render(UsageLineChart, { props: { points, timeline, viewIndex: reply } }).body;
    const rule = Number(html.match(/<line data-ts-key="rule-x[^>]*x1="([\d.]+)"/)?.[1]);
    const end = render(UsageLineChart, { props: { points, timeline, viewIndex: frames.length } }).body;
    assert.ok(rule > barX(end, 5) && rule < barX(end, 8), `the rule at ${rule} sits in the fold, not on a call`);
  });
});

describe("the token chart's axes", () => {
  // --- the duration label ---------------------------------------------------------------------------

  it("rolls every field of the duration label over into the next", () => {
    // The regression this section exists for: a session open for 3h20m05s reported "200m 05s".
    assert.equal(formatDuration(12005), "3h 20m 05s", "minutes roll over into hours");

    assert.equal(formatDuration(0), "0s", "a zero-duration run reads as zero, not blank");
    assert.equal(formatDuration(59), "59s", "under a minute stays in seconds");
    assert.equal(formatDuration(60), "1m 00s", "the minute boundary rolls exactly once");
    assert.equal(formatDuration(3599), "59m 59s", "the last second before an hour is still minutes");
    assert.equal(formatDuration(3600), "1h 00m 00s", "the hour boundary rolls exactly once");
    assert.equal(formatDuration(-5), "0s", "a negative span floors at zero rather than printing a sign");
    assert.equal(formatDuration(90.4), "1m 30s", "fractional seconds round rather than leaking decimals");

    // The axis puts these labels side by side, so a jumping field width would make the ticks ragged.
    assert.equal(formatDuration(605), "10m 05s", "the seconds field is zero-padded");
    assert.equal(formatDuration(3725), "1h 02m 05s", "the minutes field is zero-padded too");
  });

  // The same rollover one level down. The chat panel's activity feed times a single step rather
  // than a run, and it had its own formatter: milliseconds, a tenth up to ten seconds, and then a
  // copy of the two-field minutes branch that stopped there. So the header could read "3h 20m 05s"
  // while a step below it read "200m 05s" — the bug above, reintroduced by copying half of it.
  it("times a single step at step resolution and hands the rest over", () => {
    assert.equal(
      formatElapsedDuration(12_005_000),
      "3h 20m 05s",
      "a three-hour step rolls into hours rather than reporting 200 minutes"
    );

    // What is genuinely this formatter's own, and the only reason it is not just formatDuration.
    assert.equal(formatElapsedDuration(400), "400ms", "under a second is milliseconds");
    assert.equal(formatElapsedDuration(0.2), "1ms", "a delta too small to round to 1ms still reads as time spent");
    assert.equal(formatElapsedDuration(2400), "2.4s", "a step under ten seconds keeps its tenth");
    assert.equal(formatElapsedDuration(3000), "3s", "a whole second drops the pointless decimal");
    assert.equal(formatElapsedDuration(9999), "10s", "rounding up to ten hands over rather than saying 10.0s");

    // From here up it must be the shared answer exactly, or the two readings disagree again.
    assert.equal(formatElapsedDuration(45_000), formatDuration(45), "past ten seconds it is formatDuration");
    assert.equal(formatElapsedDuration(605_000), "10m 05s", "…zero-padded the same way");
  });

  // --- the axis tick form -------------------------------------------------------------------------

  it("trims only the trailing zero fields off an axis tick", () => {
    // Mirrors the x axis tick format in UsageLineChart.svelte: ticks sit on whole time steps, so the
    // trailing zero fields are dead weight on a narrow axis. The header keeps the full form.
    const tickLabel = durationTick;

    assert.equal(tickLabel(0), "0s", "the origin tick keeps its unit rather than reading as bare 0");
    assert.equal(tickLabel(3600), "1h", "a whole hour tick drops both zero fields");
    assert.equal(tickLabel(10800), "3h", "…at every hour");
    assert.equal(tickLabel(120), "2m", "a whole minute tick drops its zero seconds");
    assert.equal(tickLabel(45), "45s", "a sub-minute tick is untouched");
    assert.equal(tickLabel(3660), "1h 01m", "a tick with real minutes keeps them");
    assert.equal(tickLabel(105), "1m 45s", "a tick with real seconds keeps them");
    // Only a *trailing* run of zeros goes; a zero field with something after it has to stay, or the
    // label would claim the wrong time.
    assert.equal(tickLabel(3605), "1h 00m 05s", "a zero minutes field survives when seconds follow");
    assert.equal(tickLabel(1800), "30m", "the half-hour step reads as minutes");
  });

  // The invariant behind all of the above, swept rather than sampled: no field may ever hold a value
  // that belongs in the next one up. This is exactly what the old two-field version violated.
  it("never lets a field hold a value that belongs in the next one up", () => {
    for (let seconds = 0; seconds <= 100_000; seconds += 7) {
      const label = formatDuration(seconds);
      const [, hours, minutes, secs] =
        label.match(/^(?:(\d+)h )?(?:(\d+)m )?(\d+)s$/) ??
        assert.fail(`unparseable duration label ${label} for ${seconds}s`);
      assert.ok(Number(secs) < 60, `${label}: seconds field must stay under 60`);
      if (hours) assert.ok(Number(minutes) < 60, `${label}: minutes field must stay under 60 once hours show`);
      /* The label has to be the number it was given, not merely well shaped. */
      const round = Number(hours ?? 0) * 3600 + Number(minutes ?? 0) * 60 + Number(secs);
      assert.equal(round, seconds, `${label} does not read back as ${seconds}s`);
    }
  });
});

/* Opened under the point, the card covered the x axis and the idle-gap break the hover is read
   against. It opens above, in the top layer, and only flips when the window has more room under
   the plot, and then from the plot's bottom edge, past the axis band. */
it("opens the usage tooltip above the point, never over the axis", () => {
  const layout = slotsOf([ended(100, spent(10)), ended(110, spent(20))]);
  const { tooltip } = usageChart({ layout, plotHeight: 240, reducedMotion: true, hidden: new Set() });
  /* The host reads options only beside `use`; on a spread extension it keeps its default order. */
  assert.ok(tooltip.use && !("create" in tooltip), "options the host reads");
  assert.equal(tooltip.placement, "top");
  assert.equal(typeof tooltip.portal?.create, "function", "placed against the window, not the plot's box");

  const view = { top: 0, bottom: 900 };
  const plotBottom = 640;
  const height = 120;
  for (const pointTop of [520, 600, 639]) assert.equal(usageTipBelow(pointTop, plotBottom, height, view), false, `${pointTop}`);
  /* Scrolled to the window's top: it opens under the plot's bottom edge. */
  assert.equal(usageTipBelow(60, 300, height, view), true);
  /* No room either way: above wins when it has more, so a short window never parks it on the axis. */
  assert.equal(usageTipBelow(100, 850, height, view), false);
});

/* TanStack Charts 1.1 stopped putting its card styles on a custom tooltip body, so the custom body
   must draw its own card or the tooltip is bare text over the plot. It is the shared TipCard,
   whose surface app.css draws wherever it is. */
it("draws the usage tooltip's own card", () => {
  const source = readFileSync(new URL("./UsageLineChart.svelte", import.meta.url), "utf8");
  const body = source.match(/\{#snippet tooltipBody\([\s\S]*?\{\/snippet\}/)?.[0] ?? "";
  assert.equal(body.match(/<TipCard\b/g)?.length, 2, "both readings are the shared card");
  assert.doesNotMatch(source, /\.usage-tip/, "no second copy of the card");
  const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
  const surface = css.match(/\n\[data-tip\]::after,\n\.tip-card \{([^}]*)\}/)?.[1] ?? "";
  for (const property of ["padding", "background"]) assert.match(surface, new RegExp(`\\n\\s+${property}:`), property);
});
