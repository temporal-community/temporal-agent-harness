/**
 * Aggregating timers for tracking down where the console spends its frame budget.
 *
 * Nothing in the app calls any of this. That is the point: you add call sites
 * while you are investigating something and take them back out again, so this
 * module stays merge-clean against every branch and costs production nothing.
 *
 * Wrap the suspect and give it a size, then read the tally from the devtools
 * console:
 *
 *   import { probe } from "$lib/debug/perfProbe";
 *   replayTimeline = $derived(probe("replayTimeline", this.frames.length, () => this.#replayTimeline()));
 *   // then, in devtools:  __perf.snapshot()
 *
 * Aggregating rather than logging each call is the whole reason this exists.
 * A projection that runs 1,583 times during one session hydration produces
 * 1,583 console lines that each look survivable at 6ms; the sum that says 10.2
 * seconds is the number that names the bug.
 *
 * Set VITE_PERF_PROBE_URL to also POST batches to a collector, for runs where
 * nobody is watching the devtools console.
 *
 * The tallying has its own check: node ui/scripts/check-perf-probe.mjs. It is
 * deliberately not wired into package.json, so that this whole module adds only
 * new files and can never collide with a branch it is dropped onto.
 */

export interface ProbeStat {
  label: string;
  /** How many times the wrapped code ran. */
  calls: number;
  /** Total wall time inside it, milliseconds. */
  ms: number;
  /** Sum of the sizes passed in, so cost per item is recoverable. */
  items: number;
}

export interface ProbeMark {
  label: string;
  /** Milliseconds since the probe module loaded. */
  at: number;
  data?: Record<string, unknown>;
}

const stats = new Map<string, ProbeStat>();
const marks: ProbeMark[] = [];
const lastSeen = new Map<string, number>();

const origin = now();

function now(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

/** Time `fn`, adding to the running tally for `label`. Returns whatever it returns. */
export function probe<T>(label: string, items: number, fn: () => T): T {
  const started = now();
  try {
    return fn();
  } finally {
    const stat = stats.get(label) ?? { label, calls: 0, ms: 0, items: 0 };
    stat.calls += 1;
    stat.ms += now() - started;
    stat.items += items;
    stats.set(label, stat);
  }
}

/** Record that something happened, with optional detail. */
export function probeMark(label: string, data?: Record<string, unknown>): void {
  marks.push({ label, at: now() - origin, data });
}

/**
 * Milliseconds since the previous call with this label, or null for the first.
 * Useful for inter-arrival gaps: a stream that stalls looks identical to a
 * stream that is merely slow until you can see the gaps between frames.
 */
export function probeGap(label: string): number | null {
  const at = now();
  const previous = lastSeen.get(label);
  lastSeen.set(label, at);
  return previous == null ? null : at - previous;
}

/** Every tally, worst total first. */
export function probeSnapshot(): ProbeStat[] {
  return [...stats.values()].sort((a, b) => b.ms - a.ms);
}

export function probeMarks(): ProbeMark[] {
  return [...marks];
}

export function probeReset(): void {
  stats.clear();
  marks.length = 0;
  lastSeen.clear();
}

const probeApi = {
  snapshot: probeSnapshot,
  marks: probeMarks,
  reset: probeReset,
  /** console.table of the tally, plus cost per item. */
  table(): void {
    console.table(
      probeSnapshot().map(({ label, calls, ms, items }) => ({
        label,
        calls,
        ms: Math.round(ms),
        "ms/call": +(ms / Math.max(calls, 1)).toFixed(3),
        items
      }))
    );
  }
};

export type ProbeApi = typeof probeApi;

if (typeof window !== "undefined") {
  (window as unknown as { __perf: ProbeApi }).__perf = probeApi;

  /* Importing this module is itself the signal that someone is investigating,
     so say where the numbers are rather than making them go looking. */
  console.info("perfProbe active - read the tally with __perf.table()");

  const sink = (import.meta as { env?: Record<string, string | undefined> }).env
    ?.VITE_PERF_PROBE_URL;
  if (sink) {
    setInterval(() => {
      if (stats.size === 0 && marks.length === 0) return;
      const batch = JSON.stringify({ stats: probeSnapshot(), marks: probeMarks() });
      marks.length = 0;
      void fetch(sink, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: batch,
        keepalive: true
      }).catch(() => {
        /* A missing collector must never break the page being measured. */
      });
    }, 500);
  }
}
