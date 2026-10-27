// Timing for the cost tests: how a piece of work grows with its input, measured so that a
// sub-millisecond operation, a GC pause or a busy machine doesn't read as superlinear growth.

const median = (values) => values.sort((a, b) => a - b)[values.length >> 1];

function timeReps(fn, reps) {
  const started = performance.now();
  for (let r = 0; r < reps; r += 1) fn(r);
  return performance.now() - started;
}

/* The median of `samples` timings of one call, each sample repeating `fn` until it has run for at
   least `minMs`. `fn` gets the repetition index, so it can vary its input. */
export function msPerCall(fn, { minMs = 20, samples = 5 } = {}) {
  let reps = 1;
  for (let spent = timeReps(fn, reps); spent < minMs; spent = timeReps(fn, reps)) {
    reps = Math.ceil(reps * Math.min(10, (1.2 * minMs) / Math.max(spent, 0.05)));
  }
  return median(Array.from({ length: samples }, () => timeReps(fn, reps) / reps));
}

/* msPerCall for work that can't be repeated in place, like a fold that only moves forward: `step`
   runs the next call, and a batch is as many consecutive steps as fill `minMs`, up to `maxBatch`.
   Takes at most `maxBatch * (samples + 1)` steps. */
export function msPerStep(step, maxBatch, { minMs = 20, samples = 5 } = {}) {
  let batch = 0;
  for (const started = performance.now(); batch < maxBatch && performance.now() - started < minMs; batch += 1) step();
  return median(Array.from({ length: samples }, () => timeReps(step, batch) / batch));
}

/* The least-squares slope of log(ms) against log(size), from [size, ms] points: about 1 for
   linear work and 2 for quadratic. */
export function growthExponent(points) {
  const xs = points.map(([size]) => Math.log(size));
  const ys = points.map(([, ms]) => Math.log(ms));
  const mean = (values) => values.reduce((sum, value) => sum + value, 0) / values.length;
  const mx = mean(xs);
  const my = mean(ys);
  let covariance = 0;
  let variance = 0;
  xs.forEach((x, i) => {
    covariance += (x - mx) * (ys[i] - my);
    variance += (x - mx) ** 2;
  });
  return covariance / variance;
}
