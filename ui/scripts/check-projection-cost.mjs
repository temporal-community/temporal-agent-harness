// ABOUTME: Asserts one commit stays O(frames). #publishFrames re-runs every derived projection from
// scratch, and the live path commits once per paint, so total work is (commits x frames): linear per
// commit is what keeps a long session merely expensive instead of unusable. Doubling the events must
// roughly double the cost, never quadruple it. This is the cheap stand-in for a soak test — the
// degradation is a function of EVENT COUNT, not elapsed time, so it is reproduced by choosing n
// rather than by waiting for n to arrive.
//   node ui/scripts/check-projection-cost.mjs

import assert from "node:assert/strict";
import "./libAlias.mjs";

const { buildReplayTimeline } = await import("../src/lib/state/replayTimeline.ts");
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
   instead of the one that does.

   The subagent frames and the subagent_message_sent are load-bearing for
   buildReplayTimeline specifically, and were absent while it was quadratic: with
   one agent_id the observed-subagent lookup misses on every frame, so the whole
   run takes the cheap parent branch and the parent-turn map is never written.
   The dispatches are parent-origin (i % 25 === 6, which is never a kid frame)
   because that is what populates the map the kid frames then read, and it is
   also the shape a real session has. */
const KINDS = ["reply_delta", "tool_start", "tool_end", "model_interaction_started", "model_interaction_ended"];
const frame = (i) => {
  const kind =
    i % 50 === 0 ? "turn_started" : i % 25 === 6 ? "subagent_message_sent" : KINDS[i % KINDS.length];
  const isChild = i % 5 === 2;
  return {
    event: kind,
    data: {
      type: kind, agent_id: isChild ? "kid" : "root",
      turn_id: `t${(i / 50) | 0}`, turn_number: 1 + ((i / 50) | 0),
      timestamp: i * 0.01, resume_offset: i, event_offset: i, delta: `token ${i}`,
      user_message: `ask ${i}`, tool_id: `tool-${(i / 5) | 0}`, tool_name: "search",
      subagent_id: "kid", subagent_turn: 1 + ((i / 25) | 0), workflow_id: "wf-kid",
      model: "gemini-3.5-flash"
    }
  };
};

const SESSION = { workflow_id: "wf-root", agent_workflow_type: "QaAgent", created_at: 1 };
const SUBAGENTS = [{ subagentId: "kid", workflowId: "wf-kid", label: "QA (kid)" }];

/* Exactly what #publishFrames triggers: every derived projection, rebuilt.
   buildReplayTimeline is called here rather than hoisted into build(), and that
   is the whole point of it being in this file: it is a $derived on frames, so a
   commit rebuilds it too. This check previously handed the downstream builders a
   timeline it had assembled itself with .map(), which meant the one projection
   that had actually gone quadratic was the one projection never being run —
   every other builder was measured against its output while it sat outside the
   clock. A projection that is not called cannot be caught. */
const commit = (frames) => {
  const timeline = buildReplayTimeline(SESSION, frames, SUBAGENTS, "Agent");
  buildReplayLog(timeline);
  buildStepTimeline(timeline);
  buildTranscript(frames);
  buildAgentTreeGraph([{ workflowId: "wf-root", role: "parent", label: "A", frames, agentInterface: [] }]);
  summarizeCost(frames);
};

const build = (n) => Array.from({ length: n }, (_, i) => frame(i));

/* Best of three rounds rather than one round's mean. The same tree measures 47ms
   on a quiet machine and 78ms with other work running, so one round cannot pin a
   constant tightly enough to assert on: the threshold would have to be loose
   enough to tolerate the contention, which is loose enough to miss a regression.
   The fastest round is the one the scheduler interfered with least, and that is
   the number that is a property of the code. */
const measure = (n, reps) => {
  const frames = build(n);
  commit(frames); // shape the hidden classes before the clock starts
  let best = Infinity;
  for (let round = 0; round < 3; round += 1) {
    const started = performance.now();
    for (let r = 0; r < reps; r += 1) commit(frames);
    best = Math.min(best, (performance.now() - started) / reps);
  }
  return best;
};

/* An unmeasured warm-up. Without it the first size pays for JIT compilation and
   reads as the SLOWEST per-frame, which inverts the very ratio being asserted. */
{
  const frames = build(2_000);
  for (let r = 0; r < 40; r += 1) commit(frames);
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

/* The absolute constant is REPORTED, not asserted. This used to be a hard
   ceiling of 120ms, and downgrading it is deliberate — the number is kept so the
   knowledge is not lost, but it no longer gates.

   It was not discriminating. Alternating the committed commit and this one on a
   developer laptop, 15 rounds each, the spread WITHIN a single arm was
   138-242ms; adding a whole projection moved the median by 20ms. When the noise
   is 5x the signal, a threshold cannot tell a regression from the scheduler, and
   every number it produces is a coin flip dressed as a measurement. Two people
   A/B'd it independently against a pristine HEAD (233ms vs 242ms, overlapping)
   and it was failing on HEAD, unchanged, at the same time.

   A flaky gate is worse than no gate, and this file is the proof: it is the
   check that exists to catch a superlinear projection, and a genuinely quadratic
   one lived in the tree while it passed. A check people learn to re-run until it
   goes green is a check nobody reads the output of. report-file-size.mjs already
   sets the local precedent — report, do not gate.

   What that costs, honestly: the ratio cannot see a uniformly slower rebuild,
   because both sizes pay the same tax, so a constant-factor regression is now
   unguarded. That is a real loss and it is why the number is still printed. It
   is affordable today only because the constant is already known and recorded
   below, so the ceiling was re-failing an accepted debt rather than catching
   anything new.

   ponytail: reporting is the stopgap, not the answer. The absolute is
   load-sensitive because it is absolute; the fix is to make it comparative —
   measure a fixed synthetic workload in the same process as a yardstick and
   assert on commit/yardstick, which is a ratio and so immune the way the
   assertion above is. That was not built here because it cannot be calibrated on
   a machine this noisy: validating a load-normalisation scheme needs a quiet
   box, and the point of it is that we do not have one.

   ponytail: 120 records what a loaded machine cost, not a budget anyone endorsed
   — do not read it as approval of the cost. buildReplayLog is ~2.7us per frame
   and owns ~80% of the commit at 16,000 events; everything else together is a
   small remainder. It scales cleanly linearly, so this is a large constant
   factor and not a scaling bug. The live path commits once per animation frame,
   so that constant is paid per paint. Today's largest real session is 1,052
   frames, where it costs ~2.8ms: a future cliff, not a present defect. What
   walks us toward the cliff is longer-lived sessions, and the continue_as_new
   port landing now makes those viable. Upgrade path: make buildReplayLog
   incremental — append rows for arriving frames instead of rebuilding every row
   on every commit.

   Adding buildReplayTimeline did not meaningfully move this. Interleaved
   medians at 16,000 events: the projection itself is ~4ms (2.5%), and ~16ms
   (9.5%) is the mixed fixture costing buildStepTimeline more for the 3,200
   subagent-role entries it now produces. buildReplayLog's share is unchanged. */
const ADVISORY_MS = 120;
const advisory =
  large < ADVISORY_MS
    ? ""
    : `\n  NOTE: past the ${ADVISORY_MS}ms advisory — on a quiet machine that is worth a look, on a` +
      `\n  loaded one it is the machine. Not a failure either way; the ratio above is the gate.`;

console.log(
  `projection cost OK (4k ${small.toFixed(1)}ms, 16k ${large.toFixed(1)}ms, ${ratio.toFixed(1)}x)${advisory}`
);
