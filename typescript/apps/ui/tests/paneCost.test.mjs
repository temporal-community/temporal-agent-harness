// ABOUTME: Asserts each pane's data layer stays O(run) or better as a run grows: the work one live
// frame or one cursor step costs a pane, measured on a run100k-shaped run at N and 4N frames,
// must not grow more than 8x. Quadratic work would be 16x. The milliseconds are printed, not
// asserted, for the reason projectionCost.test.mjs gives. PANE_COST_N=25000 measures 25k
// against 100k; the default keeps CI quick.

import assert from "node:assert/strict";
import { afterAll, describe, it } from "vitest";

import { usagePointAt, usageTimelineBuilder } from "$lib/cost/pricing.ts";
import { categoryTotals, slotBudget, usageActivity, usageGaps, usageSlots } from "$lib/components/flow/usageChart.ts";
import { scaleScenario } from "$lib/mock/scaleScenarios.ts";
import { buildMountViews } from "$lib/state/fileMounts.ts";
import { buildAgentTreeGraph } from "$lib/state/flowProjection.ts";
import { buildReplayMarkers, formatDuration, replayLogAt, replayLogBuilder } from "$lib/state/replayLog.ts";
import { replayTimelineBuilder } from "$lib/state/replayTimeline.ts";
import {
  aggregateSpans,
  buildStepBoundaries,
  findWorkflowIdleGaps,
  liveIdleGaps,
  shownWorkflowRows,
  stepTimelineBuilder,
  timestampAtIndex
} from "$lib/state/stepTimeline.ts";
import { buildWorkflowScale, distinctTickLabels, tickLabelBoxes, workflowScaleTicks } from "$lib/state/workflowTimelineScale.ts";

const N = Number(process.env.PANE_COST_N ?? 4_000);
const scenario = scaleScenario("run100k");
const backlog = scenario.frames.slice(0, scenario.instantFrames);
assert.ok(backlog.length >= 4 * N, `run100k holds ${backlog.length} frames, short of ${4 * N}`);
const session = scenario.sessions[0];
const subagents = backlog
  .filter((frame) => frame.event === "subagent_started")
  .map((frame) => ({ subagentId: frame.data.subagent_id, workflowId: frame.data.workflow_id, label: frame.data.agent_key }));

/* Every projection a pane reads, folded the way the controller folds them, `appends` frames
   short of n so a case can time the last ones landing one at a time. */
function run(n, appends = 0) {
  const frames = backlog.slice(0, n);
  const timelineFold = replayTimelineBuilder();
  const timeline = timelineFold(session, frames, subagents, "Agent");
  const folds = { step: stepTimelineBuilder(), log: replayLogBuilder(), usage: usageTimelineBuilder() };
  const upTo = (k) => ({ timeline: timeline.slice(0, k), frames: frames.slice(0, k) });
  const head = upTo(n - appends);
  const state = { step: folds.step(head.timeline), log: folds.log(head.timeline), points: folds.usage(head.frames) };
  const append = (k) => {
    const next = upTo(k);
    state.step = folds.step(next.timeline);
    state.log = folds.log(next.timeline);
    state.points = folds.usage(next.frames);
    return state;
  };
  return { frames, timeline, state, append };
}

/* Best of three, as projectionCost does: the fastest round is the one the scheduler left alone. */
function best(fn, reps) {
  fn(0);
  let fastest = Infinity;
  for (let round = 0; round < 3; round += 1) {
    const started = performance.now();
    for (let r = 0; r < reps; r += 1) fn(r);
    fastest = Math.min(fastest, (performance.now() - started) / reps);
  }
  return fastest;
}

/* A live frame's cost to a pane: the folds take the frame, then the pane reads them. Once
   each, since a fold only goes forward; the median of the appends. */
function perAppend(n, read, appends = 24) {
  const { append } = run(n, appends);
  const times = [];
  for (let k = n - appends + 1; k <= n; k += 1) {
    const started = performance.now();
    read(append(k), k);
    times.push(performance.now() - started);
  }
  return times.sort((a, b) => a - b)[times.length >> 1];
}

const results = [];
function scales(pane, what, measure) {
  it(`${pane}: ${what}`, () => {
    measure(N);
    const small = measure(N);
    const large = measure(4 * N);
    const ratio = large / Math.max(small, 0.001);
    results.push({ pane, what, small, large, ratio });
    /* Under half a millisecond at 4N, the ratio is timer noise, not a shape. */
    if (large < 0.5) return;
    assert.ok(ratio < 8, `${pane} ${what}: 4x the run cost ${ratio.toFixed(1)}x (${small.toFixed(2)}ms → ${large.toFixed(2)}ms)`);
  });
}

const readTimeline = ({ step }, k) => {
  const { workflow } = step;
  const gaps = liveIdleGaps(workflow.gaps, workflow.bounds, workflow.bounds.endTs + 1);
  const scale = buildWorkflowScale(workflow.bounds, gaps, 1_200);
  tickLabelBoxes(scale, distinctTickLabels(workflowScaleTicks(scale), formatDuration), () => 40);
  shownWorkflowRows(workflow, new Map(), timestampAtIndex(workflow.points, k) ?? undefined);
};

describe("pane cost at N and 4N frames", () => {
  scales("timeline", "one live frame (fold, default folds, gaps, scale, tick boxes)", (n) => perAppend(n, readTimeline));
  scales("timeline", "one cursor step (default folds at the cursor)", (n) => {
    const { state } = run(n);
    const { workflow } = state.step;
    return best((r) => shownWorkflowRows(workflow, new Map(), timestampAtIndex(workflow.points, n - 50 * r) ?? undefined), 20);
  });
  scales("timeline", "idle gaps recomputed from every row", (n) => {
    const { workflow } = run(n).state.step;
    const rows = workflow.rows();
    return best(() => findWorkflowIdleGaps(rows, workflow.bounds), 5);
  });

  scales("tokens", "one live frame (fold, slots, activity, gaps)", (n) =>
    perAppend(n, ({ step, points }) => {
      const origin = points[0].timestamp;
      const layout = usageSlots(points, slotBudget(1_200));
      usageActivity(step, origin, layout.span);
      usageGaps(step, origin);
    })
  );
  scales("tokens", "one cursor step (slots cut at the cursor, category totals)", (n) => {
    const { points } = run(n).state;
    return best((r) => {
      const at = n - 50 * r;
      usageSlots(points, slotBudget(1_200), at);
      categoryTotals(points, at);
      usagePointAt(points, at);
    }, 10);
  });

  scales("waterfall", "one live frame (fold, span roll-up, step boundaries)", (n) =>
    perAppend(n, ({ step }) => {
      aggregateSpans(step);
      buildStepBoundaries(step);
    })
  );

  scales("logs", "one live frame (fold, anomaly markers)", (n) => perAppend(n, ({ log }) => buildReplayMarkers(log)));
  scales("logs", "one cursor step (the log as of the cursor)", (n) => {
    const { state, timeline } = run(n);
    return best((r) => replayLogAt(state.log, timeline, n - 50 * (r + 1)), 10);
  });

  scales("state flow", "one live frame (the graph, rebuilt)", (n) => {
    const { timeline } = run(n);
    const children = new Map();
    for (const entry of timeline) {
      if (entry.role === "parent") continue;
      if (!children.has(entry.workflowId)) children.set(entry.workflowId, []);
      children.get(entry.workflowId).push(entry.frame);
    }
    const sources = [
      { workflowId: session.workflow_id, role: "parent", label: "Agent", frames: timeline.filter((entry) => entry.role === "parent").map((entry) => entry.frame), agentInterface: [] },
      ...subagents.filter((agent) => children.has(agent.workflowId)).map((agent) => ({
        workflowId: agent.workflowId,
        role: "subagent",
        label: agent.label,
        parentWorkflowId: session.workflow_id,
        subagentId: agent.subagentId,
        agentKey: agent.label,
        frames: children.get(agent.workflowId),
        agentInterface: []
      }))
    ];
    return best(() => buildAgentTreeGraph(sources, { focus: true }), 2);
  });

  scales("files", "mount trees at 1,250 and 5,000 files", (n) => {
    const count = Math.round((n / N) * 1_250);
    const files = Object.fromEntries(Array.from({ length: count }, (_, k) => [`dir-${k % 40}/sub-${k % 7}/file-${k}.md`, { content: "# x", encoding: "utf-8", size: 3 }]));
    const docs = [{ key: "wf:skills", stateId: "skills", workflowId: "wf", label: "a", role: "parent", version: 1, value: { kind: "file_tree", mount: "/skills", description: "", read_only: false, directories: [], files }, changed: [], commits: 1, turnNumber: 1, problem: null }];
    return best(() => buildMountViews(docs), 3);
  });

  afterAll(() => {
    console.log(
      `pane cost, ${N.toLocaleString()} vs ${(4 * N).toLocaleString()} frames:\n` +
        results.map(({ pane, what, small, large, ratio }) => `  ${pane} / ${what}: ${small.toFixed(2)}ms → ${large.toFixed(2)}ms (${ratio.toFixed(1)}x)`).join("\n")
    );
  });
});
