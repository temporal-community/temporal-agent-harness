// A flush costs the same late in a long session as early in a short one: nothing in the core or
// the plain state walks or copies everything received so far on every flush.

import assert from "node:assert/strict";
import { test } from "node:test";

import { measure } from "./scale.ts";

test("the cost of a flush does not grow with the session", async () => {
  const short = await measure(5_000);
  const long = await measure(50_000);
  // A ratio, not a time, so a slow machine passes; a per-flush walk makes it about 7.
  const ratio = long.tailBatchMs / short.tailBatchMs;
  assert.ok(ratio < 3, `a flush at 50k frames cost ${ratio.toFixed(1)}x one at 5k`);
});
