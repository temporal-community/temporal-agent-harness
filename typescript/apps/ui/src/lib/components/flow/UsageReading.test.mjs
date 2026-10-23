// ABOUTME: Asserts the one property the token reading has to hold for a run this console read no
// events of: it must not render a zero. Every figure in UsageReading is a sum over the frames in
// hand, and a finished run whose stream Temporal cannot replay leaves none — so the sums are empty
// and `0 tok` drawn in the affirmative accent is a confident measurement of nothing, sitting beside
// runs that really did the work. Zero is a fact; "we cannot know" is a different fact. This is the
// check that fails when a figure stops going through figure(), or when the affirmative tone comes
// back.
//
// It also pins the second half of that reading: the money is off the screen. The estimate came from
// a hardcoded per-million price table nobody wants to maintain in the frontend, so pricing.ts keeps
// computing it and the UI renders none of it — no dollar figure, and no "no price configured" note
// hedging a figure that is no longer there. pricing.ts itself is unchanged and still covered by
// pricing.test.mjs.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { buildUsageTimeline, summarizeCost } from "$lib/cost/pricing.ts";
import { scaleScenario } from "$lib/mock/scaleScenarios.ts";
import { TOKEN_CATEGORY_STYLES } from "./usageChart.ts";
import UsageReading from "./UsageReading.svelte";

const COMPONENT = fileURLToPath(new URL("./UsageReading.svelte", import.meta.url));

const PRICED = "gemini-3.5-flash";
const UNPRICED = "some-model-we-hold-no-price-for";

let clock = 0;
const ended = (model, usage) => ({
  event: "model_interaction_ended",
  data: { type: "model_interaction_ended", timestamp: (clock += 1), model, usage }
});

const spent = {
  input_tokens: 1000,
  output_tokens: 400,
  thought_tokens: 600,
  cached_tokens: 250,
  tool_use_tokens: 90,
  total_tokens: 2090
};

/* An unmeasured run's `frames` are empty, so its summary is the empty sum — which
   is exactly the summary of a run that made no model calls at all. That the two
   are indistinguishable in the data is the whole reason the flag has to be passed
   in separately, and the reason the reading cannot work this out for itself. */
const NOTHING_READ = summarizeCost([]);
const REAL_SPEND = summarizeCost([ended(PRICED, spent)]);
const UNPRICED_SPEND = summarizeCost([ended(UNPRICED, spent)]);

/* A run that has made a call, so the reading draws its figures rather than the
   empty state; each test sets the at-cursor summary it is about. */
const A_CALL = buildUsageTimeline([ended(PRICED, spent)]);
const shown = (props) =>
  render(UsageReading, {
    props: { usageTimeline: A_CALL, viewIndex: 1, ...props }
  }).body;

/* Every figure, in DOM order: the headline total and cache hit, then the call's
   disjoint parts under it — here Gemini's convention, thought and tool-use beside
   output, so 1,000 input with 250 cached is 750 uncached. The per-model list renders `dd`s of its own further down, so
   only the figures column is read.

   All of them are reachable because the reading is a pane rather than a popover: it
   has no open/closed state for SSR to be unable to set, so nothing here has to
   stand in a source claim for a figure a closed render would have hidden. */
const figures = (html) => {
  const column = html.slice(html.indexOf("usage-metrics"), html.indexOf("usage-detail") >>> 0);
  return [...column.matchAll(/<dd[^>]*>([^<]*)</g)].map((m) => m[1].trim());
};

/* The tone on the headline metric, which is the first one and the affirmative one.
   The delimiter matters: the compiler appends its scoped hash inside the same
   attribute, so an anchored closing quote matches nothing. */
const headlineTone = (html) => html.match(/class="metric ([a-z]+)[\s"]/)?.[1] ?? null;

/* What a reader actually sees: text nodes only. Everything the compiler adds
   carries digits of its own — scoped class hashes, hydration markers — so "no zero
   anywhere" has to be asserted over the words rather than over the markup around
   them. One pass is enough for all of it: the markers are `<!--[-1-->`-shaped and
   hold no `>` of their own, so they fall to the same sweep as the tags, and the
   prose comments in the source never reach the render to begin with. */
const text = (html) =>
  html
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();

describe("UsageReading", () => {
  // The control. Without it, a component that dashed unconditionally would pass
  // every assertion below.
  it("a run that really spent money still says so", () => {
    const html = shown({ usage: REAL_SPEND });
    assert.deepEqual(
      figures(html),
      ["2,090", "20%", "750", "250", "400", "600", "90"],
      "a measured run reports its total, cache hit rate, then the parts that add up to it"
    );
    assert.equal(
      headlineTone(html),
      "strong",
      "and keeps the affirmative tone it has earned — `strong` draws the total in --accent"
    );
    assert.doesNotMatch(
      text(html),
      /\$/,
      "...and no dollar figure: the estimate behind it came from a hardcoded price table, so it " +
        "is computed and not shown"
    );
  });

  // A closed model call with `usage: null` is not the empty sum of a run that
  // has not called a model yet, and it is not the replay failure either.
  it("a model call that reported no usage is unknown, not zero", () => {
    const html = shown({ usage: summarizeCost([ended(PRICED, spent), ended(PRICED, null)]) });
    assert.deepEqual(
      figures(html),
      ["≥ 2,090", "≥ 20%", "≥ 750", "≥ 250", "≥ 400", "≥ 600", "≥ 90"],
      "the reported calls' sum is shown as the floor it is, not dashed away and not as a total"
    );
    assert.match(html, /data-tip="A model call ended without reporting token usage/, "each floor explains itself");
    assert.match(text(html), /Unknown, not zero: a model call ended without reporting token usage/);
    assert.doesNotMatch(text(html), /cannot replay/, "the note must not blame Temporal for it");
    assert.notEqual(headlineTone(html), "strong");

    const strip = readFileSync(new URL("../primitives/MetricStrip.svelte", import.meta.url), "utf8");
    assert.match(strip, /dd \{\s*direction: ltr;\s*unicode-bidi: isolate;/, "so a right-to-left page cannot mirror ≥ into ≤");

    const nothing = shown({ usage: summarizeCost([ended(PRICED, null)]), viewIndex: 0 });
    assert.deepEqual(figures(nothing), ["—", "—", "—", "—", "—", "—"], "with nothing reported there is no floor to show");
  });

  it("shows cache hit rate only when cache fields were reported", () => {
    const shownRate = shown({ usage: REAL_SPEND });
    assert.match(text(shownRate), /cache hit 20%/);
    assert.match(shownRate, /data-tip="Cached ÷ \(input \+ cached\)/);

    const noCacheField = summarizeCost([ended(PRICED, { input_tokens: 100, output_tokens: 20 })]);
    assert.doesNotMatch(text(shown({ usage: noCacheField })), /cache hit/);
  });

  it("uses the multimodel fixture's cached field for its rate", () => {
    const summary = summarizeCost(scaleScenario("tokens").frames);
    assert.equal(summary.tokens.cached, 1_197_800);
    assert.match(text(shown({ usage: summary })), /cache hit 11%/);
  });

  /* No call at all in the whole run: a wall of zeros over an empty axis is what
     the pane used to show, and it reads as broken rather than as "not yet". */
  it("a run with no model calls shows the shared empty state", () => {
    const html = shown({ usage: NOTHING_READ, usageTimeline: [] });
    assert.match(text(html), /No model calls yet/);
    assert.match(html, /class="empty-state/, "through the shared EmptyState");
    assert.deepEqual(figures(html), [], "no zero wall");
    assert.doesNotMatch(html, /<svg/, "and no empty axis");
  });

  /* Wide used to be two columns. The figures sat at the top of the first, and the
     chart beside them took the pane, so the rest of that column was empty. The
     reading is a header row at every width — there is no side column left to be
     empty, and the chart still takes the height under the figures. */
  it("keeps the reading a header row, so a wide pane has no empty side column", () => {
    const reading = readFileSync(COMPONENT, "utf8");
    const body = reading.match(/\n {2}\.usage-reading-body \{([^}]*)\}/)?.[1] ?? "";
    assert.match(body, /grid-template-columns:\s*minmax\(0,\s*1fr\)/, "one column at every width");
    assert.match(body, /grid-template-rows:\s*auto\s+1fr/, "the figures take their height and the chart the rest");
    assert.doesNotMatch(reading, /0\.8fr/, "the figures' column no longer grows with the pane");
    assert.doesNotMatch(reading, /@container/, "a wide container does not bring the side column back");
    const metrics = reading.indexOf('class="usage-metrics"');
    const detail = reading.indexOf('class="usage-detail"');
    assert.ok(metrics !== -1 && detail > metrics, "the figures stay above the chart");
  });

  /* The plot used to grow into the pane's leftover height, so four calls in a tall
     pane drew as a 620px cliff. Its height now follows the pane's width between a
     floor and a ceiling, and a tall pane gives the room to the model list. */
  it("bounds the chart's height instead of following the pane's", () => {
    const chart = readFileSync(new URL("./UsageLineChart.svelte", import.meta.url), "utf8");
    const rule = (source, selector) => source.match(new RegExp(`\\n {2}${selector} \\{([^}]*)\\}`))?.[1] ?? "";
    const plot = rule(chart, "\\.plot");
    assert.match(plot, /height: clamp\(var\(--plot-min\), \d+cqi, var\(--plot-max\)\)/, "sized by width, between two bounds");
    for (const box of [plot, rule(chart, "\\.usage-chart")]) {
      assert.match(box, /flex: none/, "neither box grows into the pane's height");
      assert.doesNotMatch(box, /flex: 1|min-height/);
    }
    const min = Number(chart.match(/const PLOT_MIN = (\d+);/)?.[1]);
    const max = Number(chart.match(/const PLOT_MAX = (\d+);/)?.[1]);
    assert.ok(min >= 120 && max <= 280 && min < max, `${min}–${max}px`);
    assert.match(chart, /Math\.min\(PLOT_MAX, Math\.max\(PLOT_MIN, Math\.round\(plotBox\)\)\)/, "the renderer is handed a bounded height too");
    assert.match(chart, /\.plot :global\(\.usage-plot\) \{\s*position: absolute !important;/, "the SVG never props its box open");
  });

  /* Static numbers are not controls, so they carry no box: the strip is `bare`. */
  it("draws the figures without boxes and names the parts plainly", () => {
    const html = shown({ usage: REAL_SPEND });
    const strips = [...html.matchAll(/<dl class="metric-strip([^"]*)"/g)].map((m) => m[1]);
    assert.equal(strips.length, 2);
    for (const classes of strips) assert.match(classes, /\bbare\b/, "every figure strip is bare");
    assert.match(text(html), /Breakdown/);
    assert.doesNotMatch(text(html), /overlapping parts/i, "the jargon label is gone");
    assert.match(html, /tabindex="0"[^>]*data-tip="Disjoint parts of each call/, "the explanation is a keyboard-reachable tip");
    const source = readFileSync(COMPONENT, "utf8");
    assert.doesNotMatch(source, /underline dotted/, "no dotted-underline affordance");
  });

  // The adjacent state this must not swallow: a run that has genuinely made no
  // model call yet has measured zero, and zero is the honest reading of it.
  it("a real zero is still a zero", () => {
    assert.deepEqual(
      figures(shown({ usage: NOTHING_READ, viewIndex: 0 })),
      ["0", "0", "0", "0", "0", "0"],
      "a run with no model calls yet has measured nothing; that is a fact"
    );
  });

  // The property. This is the assertion that fails without the fix.
  it("an unmeasured run renders no zero", () => {
    const html = shown({ usage: NOTHING_READ, unmeasured: true });

    assert.deepEqual(
      figures(html),
      ["—", "—", "—", "—", "—", "—"],
      "every figure for an unmeasured run is absent, not zero"
    );
    /* A panel of bare dashes is a broken panel until something says otherwise. In
       the popover this sentence was a click away and the chip carried it as a tip;
       here it takes the space the chart and the model list have vacated. */
    assert.match(
      text(html),
      /Unknown, not zero/,
      "the dashes must be accompanied by the sentence that explains them"
    );
    assert.doesNotMatch(
      text(html),
      /\d/,
      "an unmeasured run must not render a digit anywhere: the totals are unknown, not zero"
    );
    assert.notEqual(
      headlineTone(html),
      "strong",
      "the affirmative accent is a claim about a figure, and an unmeasured run has none to make"
    );
  });

  /* Partial history reaches the same state with real numbers in hand — a session
     switch hydrates a cached prefix and then learns the rest cannot be replayed —
     and those numbers are a lower bound, not a total. Same answer. */
  it("a lower bound is not a total either", () => {
    assert.deepEqual(
      figures(shown({ usage: REAL_SPEND, unmeasured: true })),
      ["—", "—", "—", "—", "—", "—", "—"],
      "a partially cached unmeasured run shows a lower bound as a figure, so it shows no figure"
    );
  });

  // With the money off the screen there is exactly one reason a figure here can be
  // unknown, so no two hedges can stack: a token count needs no price, an unpriced
  // run is fully measured and has nothing to explain, and the note pricing.ts still
  // composes for a missing cost must not surface anywhere.
  it("one absence, one hedge", () => {
    const unpriced = shown({ usage: UNPRICED_SPEND });
    assert.deepEqual(
      figures(unpriced),
      ["2,090", "20%", "750", "250", "400", "600", "90"],
      "a model we hold no price for still has an exact token count"
    );
    assert.doesNotMatch(
      unpriced,
      /No price configured/,
      "with no cost on screen there is no missing cost to hedge, so the unpriced note is gone too"
    );

    const both = shown({ usage: UNPRICED_SPEND, unmeasured: true });
    assert.match(both, /Unknown, not zero/, "an unmeasured run says why its figures are absent");
    assert.doesNotMatch(both, /No price configured/, "and says it once");
  });

  /* The reference reading: each part keyed by its bar color, a part some model never
     reports marked and footnoted rather than counted as zero, and the decision
     model's tokens as a share of the total. */
  it("keys each part to its bar color, footnotes an unreported part, and gives decisions a share", () => {
    const frames = [
      ended(PRICED, spent),
      /* OpenAI's convention: thought inside output, so output is 300 - 100, and no tool-use field. */
      ended("gpt-5.4-mini", { input_tokens: 500, output_tokens: 300, thought_tokens: 100, cached_tokens: 0, tool_use_tokens: null, total_tokens: 800 }),
      {
        event: "auto_approval_evaluation_ended",
        data: {
          type: "auto_approval_evaluation_ended",
          timestamp: (clock += 1),
          evaluation_id: "e1",
          tool_id: "t1",
          tool_name: "write_note",
          evaluator: "jev_evaluator",
          verdict: "approve",
          reason: "",
          details: { model: "jev-1.13.0", usage: { input_tokens: 100, output_tokens: 10, total_tokens: 110 } }
        }
      }
    ];
    const html = render(UsageReading, {
      props: { usage: summarizeCost(frames), usageTimeline: buildUsageTimeline(frames), viewIndex: 3 }
    }).body;
    assert.deepEqual(figures(html).slice(2), ["1,250", "250", "600", "700", "90*", "110 (3.7%)"]);
    const labels = [...html.matchAll(/<dt[^>]*>([^<]*)</g)].map((m) => m[1].trim());
    assert.deepEqual(labels.slice(2), ["input (uncached)", "cached", "output", "thought", "tool-use", "decisions"], "in stacking order");
    const swatches = [...html.matchAll(/class="metric-swatch[^"]*" aria-hidden="true" style="background: ([^;"]+)/g)].map((m) => m[1]);
    assert.deepEqual(swatches, Object.values(TOKEN_CATEGORY_STYLES).map((style) => style.color), "a square key per part, in its bar color");
    assert.match(text(html), /\* tool-use: not reported by gpt-5\.4-mini/);
    assert.match(html, /data-tip="tool-use: not reported by gpt-5\.4-mini"/, "the figure's tip says it too");
    assert.match(html, /<dl class="metric-strip[^"]*\bgrid\b/, "the parts are one grid, which is the legend");

    const early = render(UsageReading, {
      props: { usage: summarizeCost(frames.slice(0, 1)), usageTimeline: buildUsageTimeline(frames), viewIndex: 1 }
    }).body;
    assert.deepEqual(figures(early).slice(2), ["750", "250", "400", "600", "90*", "0 (0%)"], "values at the cursor, columns from the run");
  });

  // The renders above already reach every figure, so this does not stand in for
  // any of them. It is here for the one thing they cannot catch: a NEW figure
  // added later. The formatter is only ever PASSED to
  // figure(), never called, so a new reading taken straight from formatTokens()
  // fails here rather than waiting for someone to notice a zero.
  it("every figure goes through figure()", () => {
    const source = readFileSync(COMPONENT, "utf8");
    assert.equal(
      source.match(/\bformatTokens\(/g),
      null,
      "formatTokens is called directly, so that figure bypasses the unmeasured dash"
    );
    assert.equal(
      source.match(/(?<!function )\bfigure\(/g).length,
      2,
      "the count is part of the claim: 1 headline metric and 1 per token category. If a figure was " +
        "added or removed here, say so — and add it to the deepEqual assertions above"
    );
    assert.doesNotMatch(
      source,
      /formatCost|unpricedNote|unpricedModels|estimatedCostUsd/,
      "the reading reaches for a cost figure again; pricing.ts still computes one, on purpose, " +
        "but nothing here may render it"
    );
  });
});
