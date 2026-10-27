// ABOUTME: Asserts each pane's data layer stays O(run) or better as a run grows: the work one live
// frame or one cursor step costs a pane, measured on a run100k-shaped run at N, 2N, 4N and 8N
// frames, must grow as n^1.5 or slower. Linear work fits about 1 and quadratic about 2. The
// milliseconds are printed, not asserted, for the reason projectionCost.test.mjs gives.
// PANE_COST_N=10000 measures 10k to 80k; the default keeps CI quick.

import assert from "node:assert/strict";
import { afterAll, describe, it } from "vitest";

import { growthExponent, msPerCall, msPerStep } from "./support/growth.mjs";

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
const SIZES = [N, 2 * N, 4 * N, 8 * N];
const scenario = scaleScenario("run100k");
const backlog = scenario.frames.slice(0, scenario.instantFrames);
assert.ok(backlog.length >= 9 * N, `run100k holds ${backlog.length} frames, short of ${9 * N}`);
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

/* A live frame's cost to a pane: the folds take the frame, then the pane reads them. A fold only
   goes forward, so each sample is a batch of consecutive appends, and the batches straddle n by
   the same fraction at every size. */
function perAppend(n, read) {
  const maxBatch = Math.ceil(n / 40);
  const steps = maxBatch * 6;
  const end = n + Math.floor(steps / 2);
  const { append } = run(end, steps);
  let k = end - steps;
  return msPerStep(() => {
    k += 1;
    read(append(k), k);
  }, maxBatch);
}

const points = (sizes) => sizes.map(([n, ms]) => `${n.toLocaleString()}: ${ms.toFixed(3)}ms`).join(", ");
const results = [];
function scales(pane, what, measure) {
  it(`${pane}: ${what}`, () => {
    measure(N);
    const sizes = SIZES.map((n) => [n, measure(n)]);
    const exponent = growthExponent(sizes);
    results.push({ pane, what, sizes, exponent });
    assert.ok(exponent < 1.5, `${pane} ${what}: grows as n^${exponent.toFixed(2)} (${points(sizes)})`);
  });
}

const readTimeline = ({ step }, k) => {
  const { workflow } = step;
  const gaps = liveIdleGaps(workflow.gaps, workflow.bounds, workflow.bounds.endTs + 1);
  const scale = buildWorkflowScale(workflow.bounds, gaps, 1_200);
  tickLabelBoxes(scale, distinctTickLabels(workflowScaleTicks(scale), formatDuration), () => 40);
  shownWorkflowRows(workflow, new Map(), timestampAtIndex(workflow.points, k) ?? undefined);
};

describe("pane cost from N to 8N frames", () => {
  scales("timeline", "one live frame (fold, default folds, gaps, scale, tick boxes)", (n) => perAppend(n, readTimeline));
  scales("timeline", "one cursor step (default folds at the cursor)", (n) => {
    const { state } = run(n);
    const { workflow } = state.step;
    return msPerCall((r) => shownWorkflowRows(workflow, new Map(), timestampAtIndex(workflow.points, n - 50 * (r % 20)) ?? undefined));
  });
  scales("timeline", "idle gaps recomputed from every row", (n) => {
    const { workflow } = run(n).state.step;
    const rows = workflow.rows();
    return msPerCall(() => findWorkflowIdleGaps(rows, workflow.bounds));
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
    return msPerCall((r) => {
      const at = n - 50 * (r % 10);
      usageSlots(points, slotBudget(1_200), at);
      categoryTotals(points, at);
      usagePointAt(points, at);
    });
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
    return msPerCall((r) => replayLogAt(state.log, timeline, n - 50 * ((r % 10) + 1)));
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
    return msPerCall(() => buildAgentTreeGraph(sources, { focus: true }));
  });

  scales("files", "mount trees from 1,250 to 10,000 files", (n) => {
    const count = Math.round((n / N) * 1_250);
    const files = Object.fromEntries(Array.from({ length: count }, (_, k) => [`dir-${k % 40}/sub-${k % 7}/file-${k}.md`, { content: "# x", encoding: "utf-8", size: 3 }]));
    const docs = [{ key: "wf:skills", stateId: "skills", workflowId: "wf", label: "a", role: "parent", version: 1, value: { kind: "file_tree", mount: "/skills", description: "", read_only: false, directories: [], files }, changed: [], commits: 1, turnNumber: 1, problem: null }];
    return msPerCall(() => buildMountViews(docs));
  });

  afterAll(() => {
    console.log(
      `pane cost from ${N.toLocaleString()} to ${(8 * N).toLocaleString()} frames:\n` +
        results.map(({ pane, what, sizes, exponent }) => `  ${pane} / ${what}: n^${exponent.toFixed(2)} (${points(sizes)})`).join("\n")
    );
  });
});
