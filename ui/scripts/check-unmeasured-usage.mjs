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
// check-usage-totals.mjs.
//   node ui/scripts/check-unmeasured-usage.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import "./libAlias.mjs";
import "./svelteLoader.mjs";
import { render } from "svelte/server";

const COMPONENT = fileURLToPath(
  new URL("../src/lib/components/flow/UsageReading.svelte", import.meta.url)
);

const UsageReading = (await import("../src/lib/components/flow/UsageReading.svelte")).default;
const { summarizeCost, buildUsageTimeline } = await import("../src/lib/cost/pricing.ts");

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

const shown = (props) =>
  render(UsageReading, {
    props: { usageTimeline: buildUsageTimeline([]), viewIndex: 0, ...props }
  }).body;

/* All six figures, in DOM order: the headline total, then the four overlapping
   parts under it. Nothing else in the reading renders a `dd` — the per-model list
   is a `ul` of spans — so this is every figure on screen.

   This used to reach only ONE of them. The reading lived inside UsagePopover,
   whose panel is behind component state SSR cannot set, so a closed render showed
   just the chip and the other five were covered by a source claim standing in for
   them. Splitting the reading out for the TOKENS pane retired that ceiling: it has
   no open/closed state, so the figures are simply there to be read. */
const figures = (html) => [...html.matchAll(/<dd[^>]*>([^<]*)</g)].map((m) => m[1].trim());

/* The tone on the headline metric, which is the first one and the affirmative one.
   The delimiter matters: the compiler appends its scoped hash inside the same
   attribute, so an anchored closing quote matches nothing. */
const headlineTone = (html) => html.match(/class="metric ([a-z]+)[\s"]/)?.[1] ?? null;

/* What a reader actually sees: text nodes only. Everything the compiler adds
   carries digits of its own — scoped class hashes, hydration markers — so "no zero
   anywhere" has to be asserted over the words rather than over the markup around
   them. */
const text = (html) =>
  html
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();

/* --- a run that really spent money still says so -------------------------- */
// The control. Without it, a component that dashed unconditionally would pass
// every assertion below.
{
  const html = shown({ usage: REAL_SPEND });
  assert.deepEqual(
    figures(html),
    ["2,090", "1,000", "400", "600", "250"],
    "a measured run reports its real token counts: total, then the overlapping parts"
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
}

/* --- a real zero is still a zero ------------------------------------------- */
// The adjacent state this must not swallow: a run that has genuinely made no
// model call yet has measured zero, and zero is the honest reading of it.
{
  assert.deepEqual(
    figures(shown({ usage: NOTHING_READ })),
    ["0", "0", "0", "0", "0"],
    "a run with no model calls yet has measured nothing; that is a fact"
  );
}

/* --- an unmeasured run renders no zero ------------------------------------- */
// The property. This is the assertion that fails without the fix.
{
  const html = shown({ usage: NOTHING_READ, unmeasured: true });

  assert.deepEqual(
    figures(html),
    ["—", "—", "—", "—", "—"],
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
}

/* Partial history reaches the same state with real numbers in hand — a session
   switch hydrates a cached prefix and then learns the rest cannot be replayed —
   and those numbers are a lower bound, not a total. Same answer. */
{
  assert.deepEqual(
    figures(shown({ usage: REAL_SPEND, unmeasured: true })),
    ["—", "—", "—", "—", "—"],
    "a partially cached unmeasured run shows a lower bound as a figure, so it shows no figure"
  );
}

/* --- one absence, one hedge ----------------------------------------------- */
// Being unpriced used to be the OTHER reason a figure here could be unknown, and
// the two hedges had to not stack. Now there is only one: a token count needs no
// price, so an unpriced run is fully measured and has nothing to explain, and the
// note that used to hedge its missing cost must not surface anywhere.
{
  const unpriced = shown({ usage: UNPRICED_SPEND });
  assert.deepEqual(
    figures(unpriced),
    ["2,090", "1,000", "400", "600", "250"],
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
}

/* --- every figure goes through figure() ----------------------------------- */
// The renders above now reach all five figures, so this is no longer standing in
// for the ones they could not see. It is kept for what it still catches that they
// cannot: a SIXTH figure added later. The formatter is only ever PASSED to
// figure(), never called, so a new reading taken straight from formatTokens()
// fails here rather than waiting for someone to notice a zero.
{
  const source = readFileSync(COMPONENT, "utf8");
  assert.equal(
    source.match(/\bformatTokens\(/g),
    null,
    "formatTokens is called directly, so that figure bypasses the unmeasured dash"
  );
  assert.equal(
    source.match(/\bfigure\(/g).length,
    5,
    "the count is part of the claim: 1 headline metric and 4 breakdown rows. If a figure was " +
      "added or removed here, say so — and add it to the deepEqual assertions above"
  );
  assert.doesNotMatch(
    source,
    /formatCost|unpricedNote|unpricedModels|estimatedCostUsd/,
    "the reading reaches for a cost figure again; pricing.ts still computes one, on purpose, " +
      "but nothing here may render it"
  );
}

console.log(
  "check-unmeasured-usage: an unmeasured run renders no zero and keeps no affirmative tone; no " +
    "cost figure is rendered at all"
);
