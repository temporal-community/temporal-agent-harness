// ABOUTME: Asserts the two things a usage figure has to get right, both of which fail silently as a
// plausible wrong number rather than as an error. First, that `tokens.total` is the PROVIDER's grand
// total and not the sum of the five fields beside it, because those five overlap — cached is a slice
// of input, and reasoning tokens are a slice of output for two of the three producers — so both
// "input + output" and "add up all five" are wrong, in opposite directions. Second, that the
// cumulative cost series only goes unknown when an unpriced interaction actually spent tokens.
// This is the check that fails when someone makes `total` sum its own breakdown.
//   node ui/scripts/check-usage-totals.mjs

import assert from "node:assert/strict";
import "./libAlias.mjs";

const { summarizeCost, buildUsageTimeline } = await import("../src/lib/cost/pricing.ts");

/* Only model_interaction_ended frames carrying usage are counted, so that is the
   only shape worth building. `type` has to be present or timestampOf() bails. */
let clock = 0;
const ended = (model, usage) => ({
  event: "model_interaction_ended",
  data: { type: "model_interaction_ended", timestamp: (clock += 1), model, usage }
});
const other = () => ({
  event: "reply_delta",
  data: { type: "reply_delta", timestamp: (clock += 1), delta: "..." }
});

const PRICED = "gemini-3.5-flash";
const UNPRICED = "some-model-we-hold-no-price-for";

const totalOf = (frames) => summarizeCost(frames).tokens.total;

/* --- tokens.total ---------------------------------------------------------- */

/* Gemini's shape, read off google/genai/_interactions/types/usage.py: cached is
   "the cached part of the prompt" and so lives INSIDE input, while thought and
   tool-use are counted outside input/output and folded into total_tokens
   ("prompt + responses + other internal tokens"). Hence 1000 + 400 + 600 + 90,
   with the 250 cached already inside the 1000. Three different answers are
   reachable here and only one is a token count:
     2090  the provider's own total                        <- correct
     1400  input + output                                  <- understates by thought + tool-use
     2340  all five fields added                           <- overstates by double-counting cached */
const gemini = {
  input_tokens: 1000,
  output_tokens: 400,
  thought_tokens: 600,
  cached_tokens: 250,
  tool_use_tokens: 90,
  total_tokens: 2090
};

assert.equal(
  totalOf([ended(PRICED, gemini)]),
  2090,
  "total must be the provider's grand total, which on a reasoning-heavy Gemini turn is well " +
    "above input + output"
);
assert.notEqual(
  totalOf([ended(PRICED, gemini)]),
  1400,
  "total must not fall back to input + output when the provider reported a total: that drops " +
    "the thought and tool-use tokens entirely"
);
assert.notEqual(
  totalOf([ended(PRICED, gemini)]),
  2340,
  "total must not be the sum of the five breakdown fields: cached is already inside input, so " +
    "adding it counts those tokens twice"
);

/* The OpenAI producer's shape, copied from the fixture in
   tests/ai_sdks/openai_agents/test_stream_observer.py, which is ground truth for
   the containment: it asserts total_tokens == 33 alongside cached == 5 and
   thought == 7, so both of those are already inside the 11 and the 22. The
   pydantic-ai fixture is the same numbers with a comment saying so out loud.
   This case is what makes "add up all five" indefensible rather than merely
   debatable — it would report 45 tokens for an interaction the provider says
   was 33, a 36% overstatement. */
const openai = {
  input_tokens: 11,
  output_tokens: 22,
  thought_tokens: 7,
  cached_tokens: 5,
  total_tokens: 33
};

assert.equal(
  totalOf([ended(PRICED, openai)]),
  33,
  "an OpenAI-shaped interaction totals what the provider said it totals"
);
assert.notEqual(
  totalOf([ended(PRICED, openai)]),
  45,
  "11 + 22 + 7 + 5 double-counts the reasoning tokens inside output and the cached tokens " +
    "inside input"
);

/* A producer that reports no grand total of its own still has to yield a number,
   and the only defensible one from the parts alone is input + output — adding
   thought or cached on top would double-count under the OpenAI convention, and
   there is nothing on the frame that says which convention applies. This is the
   pre-existing behaviour, kept as the fallback. */
assert.equal(
  totalOf([
    ended(PRICED, {
      input_tokens: 800,
      output_tokens: 300,
      thought_tokens: 120,
      cached_tokens: 60
    })
  ]),
  1100,
  "with no provider total, fall back to input + output rather than guessing a convention"
);

/* Accumulation across frames, since every figure the UI shows is an aggregate and
   a per-frame total that is right can still be summed wrongly. */
assert.equal(
  totalOf([ended(PRICED, gemini), other(), ended(UNPRICED, openai), other()]),
  2090 + 33,
  "the aggregate is the sum of the per-interaction totals, across models and past " +
    "frames that carry no usage"
);

/* The breakdown fields themselves are untouched by any of the above: they are what
   the popover lists under the total, and they stay per-class counts. */
const spread = summarizeCost([ended(PRICED, gemini), ended(PRICED, openai)]).tokens;
assert.deepEqual(
  spread,
  { input: 1011, output: 422, thought: 607, cached: 255, toolUse: 90, total: 2123 },
  "the per-class counts accumulate independently of whatever total is reported"
);

/* --- the cumulative cost series -------------------------------------------- */

const costs = (frames) => buildUsageTimeline(frames).map((point) => point.estimatedCostUsd);

/* An unpriced interaction that actually spent tokens leaves a hole in the running
   sum, and every later point is a lower bound rather than a value. Blanking from
   there on is the honest answer and is NOT the bug: the alternative is printing a
   number that quietly omits the unpriced model, which reads as complete.
   The number of points is frames + 1 — index 0 is the synthetic "start". */
const latched = costs([
  ended(PRICED, gemini),
  ended(UNPRICED, openai),
  ended(PRICED, gemini)
]);
assert.equal(latched.length, 4, "one point per frame, plus the synthetic start");
assert.ok(latched[0] === 0 && latched[1] > 0, "points before the hole keep their real number");
assert.deepEqual(
  latched.slice(2),
  [null, null],
  "once a priced-unknown interaction has spent tokens, every later cumulative sum is unknown"
);

/* The actual defect: an interaction that spent NOTHING cost nothing at any price,
   so an unpriced model with no tokens must not blank the rest of the run. Both
   the all-zero and the empty-usage forms reach here — the loop's guard is only
   that `usage` is truthy, and `{}` is. */
for (const [label, usage] of [
  ["all-zero", { input_tokens: 0, output_tokens: 0, thought_tokens: 0 }],
  ["empty", {}]
]) {
  const survived = costs([ended(UNPRICED, usage), ended(PRICED, gemini), ended(PRICED, openai)]);
  assert.equal(
    survived[1],
    0,
    `an ${label} usage from an unpriced model contributes no cost and no uncertainty`
  );
  assert.ok(
    survived[2] > 0 && survived[3] > survived[2],
    `a fully priced interaction after an ${label} unpriced one still reports its cost`
  );
}

/* The summary panel's disclosure is unaffected and still names the model, which is
   the signal the timeline lacks: buildUsageTimeline can only blank, it cannot say
   why. Pinned here so a future timeline disclosure does not quietly replace it. */
const summary = summarizeCost([ended(PRICED, gemini), ended(UNPRICED, openai)]);
assert.equal(
  summary.estimatedCostUsd,
  null,
  "a run containing an unpriced model has no total cost, only a lower bound"
);
assert.equal(
  summary.tokens.total,
  2123,
  "...while its token count stays exact, because token counts do not need a price"
);

console.log("check-usage-totals: provider totals OK, cumulative cost uncertainty OK");
