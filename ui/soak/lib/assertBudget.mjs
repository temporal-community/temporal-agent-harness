/**
 * Best-of-N timing + ratio helpers for soak budgets.
 * Mirror of the projection-cost measurement style: fastest round wins so
 * scheduler noise does not force an absurdly loose ceiling.
 */

/**
 * Run `fn` `rounds` times; return the minimum ms.
 * If `fn` returns a number, that value is the measurement (caller timed a
 * subset); otherwise the whole call is timed.
 */
export async function bestOf(rounds, fn) {
  let best = Infinity;
  for (let i = 0; i < rounds; i += 1) {
    const started = performance.now();
    const result = await fn();
    const elapsed =
      typeof result === "number" ? result : performance.now() - started;
    best = Math.min(best, elapsed);
  }
  return best;
}

/** Fail if `ratio` is at or above `maxRatio`. */
export function assertRatio(ratio, maxRatio, detail) {
  if (!(ratio < maxRatio)) {
    throw new Error(
      `cost ratio ${ratio.toFixed(2)}x must stay under ${maxRatio}x${detail ? ` — ${detail}` : ""}`
    );
  }
}

/** Fail if `ms` is at or above `ceilingMs`. */
export function assertUnder(ms, ceilingMs, label) {
  if (!(ms < ceilingMs)) {
    throw new Error(
      `${label}: ${ms.toFixed(1)}ms exceeds ${ceilingMs}ms ceiling`
    );
  }
}
