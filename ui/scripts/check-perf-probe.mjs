import assert from "node:assert/strict";

import {
  probe,
  probeGap,
  probeMark,
  probeMarks,
  probeReset,
  probeSnapshot
} from "../src/lib/debug/perfProbe.ts";

const spin = (ms) => {
  const until = performance.now() + ms;
  while (performance.now() < until) {
    /* Busy-wait: probe measures synchronous work, so sleeping would not count. */
  }
};

assert.equal(probe("passthrough", 1, () => 42), 42, "probe should return the wrapped value");

probeReset();
probe("accumulate", 10, () => spin(5));
probe("accumulate", 7, () => spin(5));
const [accumulated] = probeSnapshot();
assert.equal(accumulated.calls, 2, "repeat labels should accumulate rather than replace");
assert.equal(accumulated.items, 17, "item sizes should sum, so cost per item is recoverable");
assert.ok(
  accumulated.ms >= 9,
  `two 5ms calls should total about 10ms, got ${accumulated.ms.toFixed(1)}ms`
);

probeReset();
probe("fast", 1, () => spin(1));
probe("slow", 1, () => spin(12));
assert.deepEqual(
  probeSnapshot().map((stat) => stat.label),
  ["slow", "fast"],
  "the snapshot should lead with the worst total, which is the number being hunted"
);

probeReset();
assert.throws(
  () =>
    probe("throwing", 1, () => {
      throw new Error("boom");
    }),
  /boom/,
  "probe should not swallow errors from the code it wraps"
);
assert.equal(probeSnapshot()[0]?.calls, 1, "a throwing call should still be tallied");

probeReset();
assert.equal(probeGap("gap"), null, "the first gap has nothing to measure from");
spin(6);
const gap = probeGap("gap");
assert.ok(gap >= 5, `the second gap should be about 6ms, got ${gap?.toFixed(1)}`);

probeReset();
probeMark("marked", { detail: 1 });
assert.equal(probeMarks().length, 1, "marks should record");
assert.deepEqual(probeMarks()[0].data, { detail: 1 }, "marks should keep their detail");

probeReset();
assert.deepEqual(probeSnapshot(), [], "reset should clear the tally");
assert.deepEqual(probeMarks(), [], "reset should clear the marks");

console.log("perf probe OK");
