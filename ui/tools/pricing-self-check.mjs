/**
 * Runnable guard for the cost estimator, which the UI no longer renders.
 *
 * The dollar figures came off the screen because the estimate behind them reads a
 * hardcoded per-million price table, and nobody wants to maintain prices in the
 * frontend. The module stayed. So this covers code with no call site in the app:
 * that is the point, not an oversight — it is what keeps the retained module
 * honest if someone ever wires it back up. UsageReading's side of the same
 * decision (nothing here reaches the screen) is pinned by
 * ui/scripts/check-unmeasured-usage.mjs.
 *
 * Tracked, but outside the `check-*.mjs` glob, so `just app-check` does not run it.
 * Run with `node --experimental-strip-types ui/tools/pricing-self-check.mjs`.
 */
import assert from "node:assert/strict";
import { formatCost, unpricedModels, unpricedNote } from "../src/lib/cost/pricing.ts";

assert.equal(formatCost(null), "—", "an uncomputable cost is a dash, not a zero");

/* The threshold is at the cent: below it every figure would read as $0.00 and say
   nothing, so a sub-cent cost gets four decimals instead of two. */
assert.equal(formatCost(0), "$0.0000");
assert.equal(formatCost(0.005), "$0.0050");
assert.equal(formatCost(0.0099), "$0.0099");
assert.equal(formatCost(0.01), "$0.01");
assert.equal(formatCost(1.5), "$1.50");

/* Which models in a run we hold no price for. This is what used to drive the
   note beside the cost figure; both survive here, unrendered. */
const usage = {
  tokens: { input: 0, output: 0, thought: 0, cached: 0, toolUse: 0, total: 0 },
  estimatedCostUsd: null,
  modelBreakdown: [
    { model: "gemini-3.5-flash", tokens: null, estimatedCostUsd: 0.01 },
    { model: "some-unpriced-model", tokens: null, estimatedCostUsd: null }
  ]
};
assert.deepEqual(unpricedModels(usage), ["some-unpriced-model"]);

assert.equal(unpricedNote([]), null, "a fully priced run has nothing to disclose");
assert.match(unpricedNote(["some-unpriced-model"]), /some-unpriced-model/);

console.log("pricing self-check passed (module retained, not rendered)");
