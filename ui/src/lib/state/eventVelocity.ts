/**
 * How fast the run was going, read along the scrubber's own axis.
 *
 * The lane is indexed by published event, so "events per bucket of index" is a
 * constant and says nothing: every bucket holds the same number of them. What
 * varies is how long each stretch of the run TOOK, so the reading is events per
 * second — a burst of tool calls compresses many events into a moment and peaks,
 * an approval nobody answered stretches a handful across a minute and troughs.
 * Bucketed by index rather than by wall clock on purpose: the x axis has to be
 * the one the cues, the playhead, and the range input are already on, or the
 * ribbon would put its peaks somewhere the marks are not.
 */

/** Anything with a place in the run and a time it happened. A log row is one. */
export interface VelocityMark {
  index: number;
  timestamp: number;
}

const MAX_BUCKETS = 48;
/** Below this a bucket is measuring one or two events, which is noise. */
const MIN_EVENTS_PER_BUCKET = 4;
/** Fewer columns than this is not a shape, so nothing is drawn at all. */
const MIN_BUCKETS = 6;
/**
 * ponytail: ceiling = stream timestamps are seconds and several events really do
 * share one, so a bucket can span 0 and have no rate to report. Floored at a
 * tenth of a second, which is the finest the harness's own frames are spaced.
 * Upgrade path = millisecond timestamps on the wire, then drop the floor.
 */
const MIN_SPAN_SECONDS = 0.1;

/* Peaks stay inside the box: the ridge is stroked, and a value flat against the
   top edge loses half that stroke to the clip and reads as cropped data. */
const TOP_MARGIN = 0.04;
const PLOT_HEIGHT = 0.92;

/**
 * Events per second across the lane, normalised to its own busiest stretch.
 *
 * `scale` is the denominator the lane is drawn against, which is the held scale
 * while a pointer is on it — so the ribbon stops re-sampling at exactly the
 * moment the marks stop sliding, and events past it are left out the same way
 * the cues past it are.
 *
 * Log-compressed rather than linear: the ratio between a reply streaming and a
 * turn waiting on a human is two or three orders of magnitude, and linearly
 * scaled the whole run reads as one spike over a flat floor.
 */
export function eventVelocity(marks: VelocityMark[], scale: number): number[] {
  const buckets = Math.min(MAX_BUCKETS, Math.floor(scale / MIN_EVENTS_PER_BUCKET));
  if (buckets < MIN_BUCKETS || marks.length === 0) return [];

  const counts = new Array<number>(buckets).fill(0);
  /* Wall clock at each bucket boundary, as a step function over the marks: the
     last event at or before the boundary is the newest thing that had happened
     by the time the lane reached that point. */
  const edgeTime = new Array<number>(buckets + 1).fill(marks[0].timestamp);
  let cursor = 0;
  let seen = marks[0].timestamp;

  for (let edge = 0; edge <= buckets; edge += 1) {
    const boundary = (edge * scale) / buckets;
    while (cursor < marks.length && marks[cursor].index <= boundary) {
      seen = marks[cursor].timestamp;
      if (edge > 0) counts[edge - 1] += 1;
      cursor += 1;
    }
    edgeTime[edge] = seen;
  }

  const rates = counts.map((count, bucket) => {
    const span = Math.max(edgeTime[bucket + 1] - edgeTime[bucket], MIN_SPAN_SECONDS);
    return Math.log1p(count / span);
  });
  const peak = Math.max(...rates);
  if (peak <= 0) return [];
  return rates.map((rate) => rate / peak);
}

/** The ridge as an SVG path in a `0 0 100 1` box, or "" when there is no shape. */
export function velocityPath(values: number[]): string {
  if (values.length < 2) return "";
  return values
    .map((value, bucket) => {
      const x = (bucket / (values.length - 1)) * 100;
      const y = 1 - TOP_MARGIN - value * PLOT_HEIGHT;
      return `${bucket === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(3)}`;
    })
    .join("");
}
