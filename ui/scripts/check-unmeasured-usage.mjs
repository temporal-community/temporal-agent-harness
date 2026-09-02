// ABOUTME: Asserts the one property the usage chip has to hold for a run this console read no
// events of: it must not render a zero. Every figure in UsagePopover is a sum over the frames in
// hand, and a finished run whose stream Temporal cannot replay leaves none — so the sums are empty
// and `0 tok $0.0000` in a green success chip is a confident measurement of nothing, sitting beside
// runs that really spent money. Zero is a fact; "we cannot know" is a different fact. This is the
// check that fails when a figure stops going through figure(), when the success tone comes back, or
// when the unmeasured and unpriced hedges start stacking two explanations on one absence.
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
  ...html.matchAll(/<span class="usage-chip-(?:tokens|cost)[^"]*"[^>]*>([^<]*)</g)
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
  assert.deepEqual(
    figures(html),
    ["2,090", "$0.0014"],
    "a measured run reports its real figures"
  );
  assert.match(html, /chip[^"]*\bsuccess\b/, "and keeps the success tone it has earned");
}

/* --- a real zero is still a zero ------------------------------------------- */
// The adjacent state this must not swallow: a run that has genuinely made no
// model call yet has measured zero, and zero is the honest reading of it.
{
  assert.deepEqual(
    figures(shown({ usage: NOTHING_READ })),
    ["0", "$0.0000"],
    "a run with no model calls yet has measured nothing and spent nothing; that is a fact"
  );
}

/* --- an unmeasured run renders no zero ------------------------------------- */
// The property. This is the assertion that fails without the fix.
{
  const html = shown({ usage: NOTHING_READ, unmeasured: true });

  assert.deepEqual(figures(html), ["—", "—"], "both figures of an unmeasured run are absent");
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
    ["—", "—"],
    "a partially cached unmeasured run shows a lower bound as a figure, so it shows no figure"
  );
}

/* --- one hedge, not two --------------------------------------------------- */
// Unpriced models are the other reason a figure here can be unknown, and the two
// must not stack: being unmeasured is the wider claim, so which models we hold
// prices for is moot and its note must not appear beside the dash as a second
// explanation for one absence.
{
  const unpriced = shown({ usage: UNPRICED_SPEND });
  assert.match(unpriced, /No price configured/, "an unpriced model still explains itself");
  assert.deepEqual(
    figures(unpriced),
    ["2,090", "—"],
    "...with exact tokens and no cost, which is what unpriced means"
  );

  const both = shown({ usage: UNPRICED_SPEND, unmeasured: true });
  assert.match(both, /Unknown, not zero/, "an unmeasured run says why its figures are absent");
  assert.doesNotMatch(
    both,
    /No price configured/,
    "a run that is both unmeasured and unpriced must read as one hedge, not two"
  );
}

/* --- every figure goes through figure() ----------------------------------- */
// The two strips inside the panel are past what an SSR render of a closed popover
// can see, and they are two of the four call sites. What holds them is that the
// formatters are only ever PASSED to figure(), never called: a fifth figure added
// straight from formatTokens() would render a zero the renders above cannot
// reach, and this is what fails instead.
{
  const source = readFileSync(COMPONENT, "utf8");
  for (const formatter of ["formatTokens", "formatCost"]) {
    assert.equal(
      source.match(new RegExp(`\\b${formatter}\\(`, "g")),
      null,
      `${formatter} is called directly, so that figure bypasses the unmeasured dash`
    );
  }
  assert.equal(
    source.match(/\bfigure\(/g).length,
    8,
    "the count is part of the claim: 2 chip spans, 2 headline metrics and 4 breakdown rows. " +
      "If a figure was added or removed here, say so"
  );
}

console.log(
  "check-unmeasured-usage: an unmeasured run renders no zero, keeps no success tone, and hedges once"
);
