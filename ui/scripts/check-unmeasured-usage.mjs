// ABOUTME: Asserts the one property the usage chip has to hold for a run this console read no
// events of: it must not render a zero. Every figure in UsagePopover is a sum over the frames in
// hand, and a finished run whose stream Temporal cannot replay leaves none — so the sums are empty
// and `0 tok` in a green success chip is a confident measurement of nothing, sitting beside runs
// that really did the work. Zero is a fact; "we cannot know" is a different fact. This is the check
// that fails when a figure stops going through figure(), or when the success tone comes back.
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
  new URL("../src/lib/components/flow/UsagePopover.svelte", import.meta.url)
);

const UsagePopover = (await import("../src/lib/components/flow/UsagePopover.svelte")).default;
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
   in separately, and the reason the popover cannot work this out for itself. */
const NOTHING_READ = summarizeCost([]);
const REAL_SPEND = summarizeCost([ended(PRICED, spent)]);
const UNPRICED_SPEND = summarizeCost([ended(UNPRICED, spent)]);

const shown = (props) =>
  render(UsagePopover, {
    props: { usageTimeline: buildUsageTimeline([]), viewIndex: 0, ...props }
  }).body;

/* ponytail: ceiling = SSR renders the popover CLOSED (`open` is component state,
   not a prop), so what these renders see is the chip — which is where the
   reported bug is visible and the only part of this component a reader meets
   without clicking. The headline and breakdown strips inside the panel are
   covered by the source claim at the bottom instead. Upgrade path = a check that
   can open it, once anything in this repo can drive a component's state. */
const figures = (html) => [
  ...html.matchAll(/<span class="usage-chip-tokens[^"]*"[^>]*>([^<]*)</g)
].map((match) => match[1].trim());

/* What a reader actually sees: text nodes only. Everything the compiler adds
   carries digits of its own — scoped class hashes, hydration markers, the icon's
   viewBox, the per-instance popover id — so "no zero anywhere" has to be asserted
   over the words rather than over the markup around them. */
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
  assert.deepEqual(figures(html), ["2,090"], "a measured run reports its real token count");
  assert.match(html, /chip[^"]*\bsuccess\b/, "and keeps the success tone it has earned");
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
    ["0"],
    "a run with no model calls yet has measured nothing; that is a fact"
  );
}

/* --- an unmeasured run renders no zero ------------------------------------- */
// The property. This is the assertion that fails without the fix.
{
  const html = shown({ usage: NOTHING_READ, unmeasured: true });

  assert.deepEqual(figures(html), ["—"], "the chip's figure for an unmeasured run is absent");
  /* The sentence used to hang off the cost span, which is gone. A bare dash on a
     closed chip is a broken panel until something says otherwise, and the panel's
     copy is a click away. */
  assert.match(
    html,
    /usage-chip-tokens[^>]*data-tip="Unknown, not zero/,
    "the dash on the closed chip must carry the sentence that explains it"
  );
  assert.doesNotMatch(
    text(html),
    /\d/,
    "an unmeasured run must not render a digit anywhere: the totals are unknown, not zero"
  );
  assert.doesNotMatch(
    html,
    /chip[^"]*\bsuccess\b/,
    "a green chip is an affirmative claim, and an unmeasured run has nothing to affirm"
  );
}

/* Partial history reaches the same state with real numbers in hand — a session
   switch hydrates a cached prefix and then learns the rest cannot be replayed —
   and those numbers are a lower bound, not a total. Same answer. */
{
  assert.deepEqual(
    figures(shown({ usage: REAL_SPEND, unmeasured: true })),
    ["—"],
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
    ["2,090"],
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
// The two strips inside the panel are past what an SSR render of a closed popover
// can see, and they are five of the six call sites. What holds them is that the
// formatter is only ever PASSED to figure(), never called: a seventh figure added
// straight from formatTokens() would render a zero the renders above cannot
// reach, and this is what fails instead.
{
  const source = readFileSync(COMPONENT, "utf8");
  assert.equal(
    source.match(/\bformatTokens\(/g),
    null,
    "formatTokens is called directly, so that figure bypasses the unmeasured dash"
  );
  assert.equal(
    source.match(/\bfigure\(/g).length,
    6,
    "the count is part of the claim: 1 chip span, 1 headline metric and 4 breakdown rows. " +
      "If a figure was added or removed here, say so"
  );
  assert.doesNotMatch(
    source,
    /formatCost|unpricedNote|unpricedModels|estimatedCostUsd/,
    "the popover reaches for a cost figure again; pricing.ts still computes one, on purpose, " +
      "but nothing here may render it"
  );
}

console.log(
  "check-unmeasured-usage: an unmeasured run renders no zero and keeps no success tone; no cost " +
    "figure is rendered at all"
);
