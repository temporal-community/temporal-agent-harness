// ABOUTME: Asserts one commit stays O(frames). #publishFrames re-runs every derived projection from
// scratch, and the live path commits once per paint, so total work is (commits x frames): linear per
// commit is what keeps a long session merely expensive instead of unusable. Doubling the events must
// roughly double the cost, never quadruple it. This is the cheap stand-in for a soak test — the
// degradation is a function of EVENT COUNT, not elapsed time, so it is reproduced by choosing n
// rather than by waiting for n to arrive.
//   node ui/scripts/check-projection-cost.mjs

import assert from "node:assert/strict";
import "./libAlias.mjs";

const { buildReplayLog } = await import("../src/lib/state/replayLog.ts");
const { buildStepTimeline } = await import("../src/lib/state/stepTimeline.ts");
const { buildTranscript } = await import("../src/lib/state/transcript.ts");
const { buildAgentTreeGraph } = await import("../src/lib/state/flowProjection.ts");
const { summarizeCost } = await import("../src/lib/cost/pricing.ts");

/* A varied mix. 16k identical reply_deltas would exercise one branch and could
   stay linear while the tool and subagent paths went quadratic.

   The turn boundary every 50 frames is load-bearing, not decoration: it is what
   drives resetTurnTools(). Without it the whole run is one unbounded turn, every
   tool ever seen stays in runtimeNodeOrder, and markRuntimeNode's linear scan
   makes this measure a session that cannot happen (3,200 tools open at once)
   instead of the one that does. */
const KINDS = ["reply_delta", "tool_start", "tool_end", "model_interaction_started", "model_interaction_ended"];
const frame = (i) => {
  const kind = i % 50 === 0 ? "turn_started" : KINDS[i % KINDS.length];
  return {
    event: kind,
    data: {
      type: kind, agent_id: "root", turn_id: `t${(i / 50) | 0}`, turn_number: 1 + ((i / 50) | 0),
      timestamp: i * 0.01, resume_offset: i, event_offset: i, delta: `token ${i}`,
      user_message: `ask ${i}`, tool_id: `tool-${(i / 5) | 0}`, tool_name: "search",
      model: "gemini-3.5-flash"
    }
  };
};

/* Exactly what #publishFrames triggers: every derived projection, rebuilt. */
const commit = (frames, timeline) => {
  buildReplayLog(timeline);
  buildStepTimeline(timeline);
  buildTranscript(frames);
  buildAgentTreeGraph([{ workflowId: "wf", role: "parent", label: "A", frames, agentInterface: [] }]);
  summarizeCost(frames);
};

const build = (n) => {
  const frames = Array.from({ length: n }, (_, i) => frame(i));
  return [frames, frames.map((f) => ({ workflowId: "wf", role: "parent", label: "Agent", frame: f }))];
};

/* Best of three rounds rather than one round's mean. The same tree measures 47ms
   on a quiet machine and 78ms with other work running, so one round cannot pin a
   constant tightly enough to assert on: the threshold would have to be loose
   enough to tolerate the contention, which is loose enough to miss a regression.
   The fastest round is the one the scheduler interfered with least, and that is
   the number that is a property of the code. */
const measure = (n, reps) => {
  const [frames, timeline] = build(n);
  commit(frames, timeline); // shape the hidden classes before the clock starts
  let best = Infinity;
  for (let round = 0; round < 3; round += 1) {
    const started = performance.now();
    for (let r = 0; r < reps; r += 1) commit(frames, timeline);
    best = Math.min(best, (performance.now() - started) / reps);
  }
  return best;
};

/* An unmeasured warm-up. Without it the first size pays for JIT compilation and
   reads as the SLOWEST per-frame, which inverts the very ratio being asserted. */
{
  const [frames, timeline] = build(2_000);
  for (let r = 0; r < 40; r += 1) commit(frames, timeline);
}

const small = measure(4_000, 10);
const large = measure(16_000, 5);

/* Both thresholds below are calibrated against node, not a browser. They guard
   the SHAPE of the cost curve, which is a property of the code; the absolute
   milliseconds a real paint budget sees are the browser's, and are not what
   this file is measuring. */

/* 4x the events for ~4x the work. The bar is 8x: loose enough that a noisy CI
   box never fails it, tight enough that genuinely quadratic work (which would
   be 16x) cannot slip through. */
const ratio = large / small;
assert.ok(
  ratio < 8,
  `4x the events must not cost ${ratio.toFixed(1)}x the work — a projection went superlinear ` +
    `(4,000 events: ${small.toFixed(1)}ms, 16,000 events: ${large.toFixed(1)}ms)`
);

/* The absolute ceiling, since a uniformly slow rebuild is a hitch on every paint
   whether or not it scales cleanly. 120ms is 1.2x the slowest run observed on a
   fully loaded machine (99ms), against ~48ms on a quiet one. The gap is the
   machine, not the code: the identical commit measured 70-78ms here while a
   checkout of an older one measured 70-78ms interleaved with it. A ceiling under
   ~100 flakes on a busy dev box, so this one only catches a gross regression in
   the constant; the ratio assertion above is the one with teeth.

   ponytail: 120 records what a loaded machine costs today, not a budget anyone
   endorsed — do not read the number as approval of the cost. buildReplayLog is
   ~2.7us per frame and owns ~42ms of the ~48ms quiet-machine commit at 16,000
   events; everything else together is under 7ms. It scales cleanly linearly, so
   this is a large constant factor and not a scaling bug. The live path commits
   once per animation frame, so that constant is paid per paint. Today's largest
   real session is 1,052 frames, where the same constant costs ~2.8ms: a future
   cliff, not a present defect. What walks us toward the cliff is longer-lived
   sessions, and the continue_as_new port landing now makes those viable.
   Upgrade path: make buildReplayLog incremental — append rows for arriving
   frames instead of rebuilding every row on every commit — which should take the
   quiet-machine number far enough down that this ceiling can be both lowered and
   made tight enough to mean something. */
assert.ok(
  large < 120,
  `one commit at 16,000 events took ${large.toFixed(1)}ms; past ~16ms it drops a frame on every paint`
);

console.log(`projection cost OK (4k ${small.toFixed(1)}ms, 16k ${large.toFixed(1)}ms, ${ratio.toFixed(1)}x)`);
