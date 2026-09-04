// ABOUTME: Asserts the data mapping UsageLineChart.svelte feeds to TanStack Charts. Mirrors the
// derivations from that component, which cannot be imported here because it is Svelte-compiled.
// The properties that matter are the ones a chart engine cannot paper over: an empty series must
// short-circuit before a scale is built, a single point and an all-equal series must still produce
// a non-degenerate domain rather than dividing by zero, samples sharing one instant must collapse
// (a stacked area mark rejects a repeated x outright, which blanked the chart in the browser), and
// out-of-order timestamps must sort so the path does not double back. It also pins the duration
// label the header and the x axis both read through, which used to say "200m 05s" for a run of
// three and a bit hours because minutes never rolled over into hours.
//   node ui/scripts/check-usage-chart.mjs

import assert from "node:assert/strict";
import "./libAlias.mjs";

/* The real formatter, not a copy: the component imports this same function, so a copy here would
   keep passing after the shared one rotted. */
const { formatDuration } = await import("../src/lib/state/replayLog.ts");

// --- mirrored from UsageLineChart.svelte ---------------------------------------------------------

function hasTokens(point) {
  return point.tokens.total > 0;
}

function tokenWindow(source) {
  const firstTokenIndex = source.findIndex(hasTokens);
  if (firstTokenIndex === -1) return source;
  return source.slice(Math.max(0, firstTokenIndex - 1));
}

function collapseSameInstant(source) {
  const collapsed = [];
  for (const sample of source) {
    const previous = collapsed[collapsed.length - 1];
    if (previous && previous.elapsed === sample.elapsed) collapsed[collapsed.length - 1] = sample;
    else collapsed.push(sample);
  }
  return collapsed;
}

function toSamples(points) {
  const chartPoints = tokenWindow(points);
  const origin = chartPoints.length
    ? Math.min(...chartPoints.map((point) => point.timestamp))
    : 0;
  return collapseSameInstant(
    chartPoints
      .map((point) => ({
        index: point.index,
        elapsed: Math.max(0, point.timestamp - origin),
        tokens: point.tokens.total,
        event: point.event,
      }))
      .sort((a, b) => a.elapsed - b.elapsed || a.index - b.index)
  );
}

function domains(samples) {
  const peakTokens = samples.reduce((max, s) => Math.max(max, s.tokens), 0);
  const lastElapsed = samples.length ? samples[samples.length - 1].elapsed : 0;
  return {
    peakTokens,
    lastElapsed,
    y: [0, peakTokens > 0 ? peakTokens * 1.08 : 4],
    x: [0, lastElapsed > 0 ? lastElapsed : 1],
  };
}

function niceTokenTicks(peak, targetCount = 3) {
  if (!Number.isFinite(peak) || peak <= 0) return [0, 4];
  const rough = peak / Math.max(2, targetCount);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step =
    [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((candidate) => candidate >= rough) ??
    magnitude * 10;

  const ticks = [];
  while (ticks.length * step <= peak + 1e-9 && ticks.length <= 64) {
    ticks.push(Number((ticks.length * step).toPrecision(12)));
  }
  return ticks.length >= 2 ? ticks : [0, peak];
}

function latestSampleAtOrBefore(source, index) {
  let found;
  for (const sample of source) {
    if (sample.index <= index) found = sample;
  }
  return found ?? source[0];
}

// A domain is usable by a linear scale only if it spans a non-zero, finite width. This is the
// property that a hand-rolled `value / max` normalization silently violated.
function assertUsableDomain(domain, label) {
  const [lo, hi] = domain;
  assert.ok(Number.isFinite(lo) && Number.isFinite(hi), `${label}: domain must be finite`);
  assert.ok(hi > lo, `${label}: domain must span a non-zero width, got [${lo}, ${hi}]`);
}

// --- fixtures ------------------------------------------------------------------------------------

const point = (index, timestamp, total) => ({
  index,
  timestamp,
  event: "reply_delta",
  tokens: { input: total, output: 0, thought: 0, cached: 0, toolUse: 0, total },
  estimatedCostUsd: null,
});

// --- empty series --------------------------------------------------------------------------------

const empty = toSamples([]);
assert.equal(empty.length, 0, "an empty series produces no samples");
// The component renders its empty state instead of a chart, so nothing downstream may assume a
// sample exists. Guard the accessor the header reads.
assert.equal(
  latestSampleAtOrBefore(empty, 5),
  undefined,
  "an empty series yields no current sample rather than throwing"
);

// A series of points that never billed a token is still empty of usage, and must not be mistaken
// for a flat series at zero: tokenWindow keeps them, so the domain floor is what saves it.
const neverBilled = toSamples([point(0, 100, 0), point(1, 110, 0)]);
assertUsableDomain(domains(neverBilled).y, "all-zero series");
assert.deepEqual(
  domains(neverBilled).y,
  [0, 4],
  "an all-zero series floors to whole-token ticks instead of collapsing to [0, 0]"
);

// --- single point --------------------------------------------------------------------------------

const single = toSamples([point(0, 100, 1200)]);
assert.equal(single.length, 1, "a single billed point survives windowing");
assert.equal(single[0].elapsed, 0, "the only point sits at the time origin");
const singleDomains = domains(single);
assertUsableDomain(singleDomains.x, "single point x");
assertUsableDomain(singleDomains.y, "single point y");
assert.deepEqual(
  singleDomains.x,
  [0, 1],
  "a zero-duration run still spans a non-zero time domain"
);

// --- all-equal (flat) series ---------------------------------------------------------------------

const flat = toSamples([
  point(0, 100, 500),
  point(1, 110, 500),
  point(2, 120, 500),
  point(3, 130, 500),
]);
const flatDomains = domains(flat);
assertUsableDomain(flatDomains.y, "flat series y");
assert.ok(
  flatDomains.y[1] > 500,
  "a flat series gets headroom above its value so the line is not drawn on the frame edge"
);
// Every sample maps to the same height, which is correct — but it must be a real height, not a
// division by a zero range.
const flatHeights = flat.map((s) => (s.tokens - flatDomains.y[0]) / (flatDomains.y[1] - flatDomains.y[0]));
for (const height of flatHeights) {
  assert.ok(
    Number.isFinite(height) && height > 0 && height < 1,
    `a flat series must map inside the plot, got ${height}`
  );
}
assert.equal(new Set(flatHeights).size, 1, "a flat series maps every sample to one height");

// --- samples sharing one instant -----------------------------------------------------------------

// This is the case that blanked the chart in the browser: real streams land several frames inside
// one sampled second, and the area mark refuses a repeated x.
const sameInstant = toSamples([
  point(0, 100, 10),
  point(1, 100, 40),
  point(2, 100, 90),
  point(3, 110, 150),
]);
assert.equal(sameInstant.length, 2, "a run of same-instant samples collapses to one");
assert.equal(
  sameInstant[0].tokens,
  90,
  "the collapsed sample keeps the last cumulative total at that instant, not the first"
);
assert.equal(
  new Set(sameInstant.map((s) => s.elapsed)).size,
  sameInstant.length,
  "every emitted sample has a unique elapsed time"
);

// The invariant, stated once over a nastier series: no duplicate x ever reaches the mark.
const messy = toSamples([
  point(0, 100, 5),
  point(1, 100, 5),
  point(2, 101, 7),
  point(3, 101, 9),
  point(4, 101, 11),
  point(5, 104, 20),
]);
assert.equal(
  new Set(messy.map((s) => s.elapsed)).size,
  messy.length,
  "no duplicate elapsed value survives collapsing"
);

// --- out-of-order timestamps ---------------------------------------------------------------------

const shuffled = toSamples([point(0, 130, 900), point(1, 100, 10), point(2, 115, 400)]);
const elapsedOrder = shuffled.map((s) => s.elapsed);
assert.deepEqual(
  [...elapsedOrder].sort((a, b) => a - b),
  elapsedOrder,
  "samples are emitted in ascending time so the path cannot double back"
);
assert.equal(elapsedOrder[0], 0, "the earliest timestamp becomes the origin");

// --- the replay marker tracks the scrubber -------------------------------------------------------

const series = toSamples([
  point(0, 100, 100),
  point(4, 110, 400),
  point(9, 120, 900),
]);
assert.equal(latestSampleAtOrBefore(series, 4).tokens, 400, "the marker lands on the scrubbed step");
assert.equal(
  latestSampleAtOrBefore(series, 7).tokens,
  400,
  "between samples the marker holds the last one reached"
);
assert.equal(
  latestSampleAtOrBefore(series, 99).tokens,
  900,
  "past the end the marker rests on the final sample"
);
assert.equal(
  latestSampleAtOrBefore(series, -1).tokens,
  100,
  "before the first sample the marker falls back to the head rather than undefined"
);

// --- windowing keeps one silent point of lead-in -------------------------------------------------

const withLeadIn = toSamples([
  point(0, 100, 0),
  point(1, 110, 0),
  point(2, 120, 0),
  point(3, 130, 700),
]);
assert.equal(
  withLeadIn.length,
  2,
  "windowing trims the silent head to a single point of lead-in"
);
assert.equal(withLeadIn[0].tokens, 0, "the retained lead-in point is the silent one");
assert.equal(withLeadIn[1].tokens, 700, "the first billed point follows it");

// --- the duration label ---------------------------------------------------------------------------

// The regression this section exists for: a session open for 3h20m05s reported "200m 05s".
assert.equal(formatDuration(12005), "3h 20m 05s", "minutes roll over into hours");

assert.equal(formatDuration(0), "0s", "a zero-duration run reads as zero, not blank");
assert.equal(formatDuration(59), "59s", "under a minute stays in seconds");
assert.equal(formatDuration(60), "1m 00s", "the minute boundary rolls exactly once");
assert.equal(formatDuration(3599), "59m 59s", "the last second before an hour is still minutes");
assert.equal(formatDuration(3600), "1h 00m 00s", "the hour boundary rolls exactly once");
assert.equal(formatDuration(-5), "0s", "a negative span floors at zero rather than printing a sign");
assert.equal(formatDuration(90.4), "1m 30s", "fractional seconds round rather than leaking decimals");

// The axis puts these labels side by side, so a jumping field width would make the ticks ragged.
assert.equal(formatDuration(605), "10m 05s", "the seconds field is zero-padded");
assert.equal(formatDuration(3725), "1h 02m 05s", "the minutes field is zero-padded too");

// --- the axis tick form -------------------------------------------------------------------------

// Mirrors the x axis tick format in UsageLineChart.svelte: ticks sit on whole time steps, so the
// trailing zero fields are dead weight on a narrow axis. The header keeps the full form.
const tickLabel = (seconds) => formatDuration(seconds).replace(/( 00m)? 00s$/, "");

assert.equal(tickLabel(0), "0s", "the origin tick keeps its unit rather than reading as bare 0");
assert.equal(tickLabel(3600), "1h", "a whole hour tick drops both zero fields");
assert.equal(tickLabel(10800), "3h", "…at every hour");
assert.equal(tickLabel(120), "2m", "a whole minute tick drops its zero seconds");
assert.equal(tickLabel(45), "45s", "a sub-minute tick is untouched");
assert.equal(tickLabel(3660), "1h 01m", "a tick with real minutes keeps them");
assert.equal(tickLabel(105), "1m 45s", "a tick with real seconds keeps them");
// Only a *trailing* run of zeros goes; a zero field with something after it has to stay, or the
// label would claim the wrong time.
assert.equal(tickLabel(3605), "1h 00m 05s", "a zero minutes field survives when seconds follow");
assert.equal(tickLabel(1800), "30m", "the half-hour step reads as minutes");

// The invariant behind all of the above, swept rather than sampled: no field may ever hold a value
// that belongs in the next one up. This is exactly what the old two-field version violated.
for (let seconds = 0; seconds <= 100_000; seconds += 7) {
  const label = formatDuration(seconds);
  const [, hours, minutes, secs] =
    label.match(/^(?:(\d+)h )?(?:(\d+)m )?(\d+)s$/) ??
    assert.fail(`unparseable duration label ${label} for ${seconds}s`);
  assert.ok(Number(secs) < 60, `${label}: seconds field must stay under 60`);
  if (hours) assert.ok(Number(minutes) < 60, `${label}: minutes field must stay under 60 once hours show`);
  /* The label has to be the number it was given, not merely well shaped. */
  const round = Number(hours ?? 0) * 3600 + Number(minutes ?? 0) * 60 + Number(secs);
  assert.equal(round, seconds, `${label} does not read back as ${seconds}s`);
}

// --- the y axis ticks -----------------------------------------------------------------------------

// The regression this section exists for: `nice: true` rounds the TOP of the domain out to a whole
// tick, so a run peaking at 430,000 tokens grew a 600,000 axis and drew its series in the bottom
// 72% of the plot. Ticks are now chosen INSIDE the domain instead, and the domain keeps only its
// own 8% headroom.
{
  const ticks = niceTokenTicks(430_000);
  assert.deepEqual(ticks, [0, 200_000, 400_000], "a 430k peak reads on 200k steps, not a 600k axis");

  /* The property, swept rather than sampled: no tick may exceed the peak (that is exactly what
     grew the axis), the ticks must be evenly spaced from zero, and the series must end up using
     most of the box it is drawn in. */
  for (let peak = 1; peak < 2_000_000; peak = Math.ceil(peak * 1.37)) {
    const swept = niceTokenTicks(peak);
    const top = swept[swept.length - 1];
    assert.ok(swept.length >= 2, `peak ${peak}: an axis needs at least two labels, got ${swept}`);
    assert.equal(swept[0], 0, `peak ${peak}: the axis is zero-based, the series being cumulative`);
    assert.ok(top <= peak + 1e-9, `peak ${peak}: tick ${top} sits above the peak it labels`);

    const step = swept[1] - swept[0];
    for (let i = 1; i < swept.length; i += 1) {
      assert.ok(
        Math.abs(swept[i] - swept[i - 1] - step) < 1e-6,
        `peak ${peak}: ticks are not evenly spaced (${swept})`
      );
    }
    /* Round in the 1/2/5 family a reader recognises, so no label reads 143,333. */
    const mantissa = step / 10 ** Math.floor(Math.log10(step));
    assert.ok(
      [1, 2, 2.5, 5, 10].some((m) => Math.abs(m - mantissa) < 1e-9),
      `peak ${peak}: step ${step} is not a round one`
    );
    /* The whole point: the domain top is the peak plus its headroom, and the series has to fill
       most of that. 1/1.08 = 92.6% is the ceiling; anything under 80% is the old bug back. */
    const domainTop = peak * 1.08;
    assert.ok(
      peak / domainTop > 0.8,
      `peak ${peak}: the series only reaches ${((peak / domainTop) * 100).toFixed(0)}% of its box`
    );
    /* Two ticks minimum is not enough on its own — an axis of [0, step] with the peak far above
       is unreadable. Three or four labels is the target. */
    assert.ok(swept.length <= 6, `peak ${peak}: ${swept.length} labels is a crowded axis`);
  }

  // A run that billed nothing has no peak; the ends of the all-zero domain floor are all there is.
  assert.deepEqual(niceTokenTicks(0), [0, 4], "an unbilled run labels the domain floor, not NaN");
  assert.deepEqual(niceTokenTicks(-1), [0, 4], "a negative peak cannot reach Math.log10");
  // Small counts must still land on whole tokens rather than fractions of one.
  assert.deepEqual(niceTokenTicks(9), [0, 5], "a nine-token run steps in fives");
}

console.log("usage chart mapping ok");
