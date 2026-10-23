// Each projection folded forward a batch at a time must be the projection rebuilt from scratch,
// including after it has to start over (another session, a subagent relabeling earlier frames),
// and must never change a result it already handed out.
//   npx vitest run src/lib/state/incrementalProjections.test.mjs
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildUsageTimeline, usageTimelineBuilder } from "../cost/pricing.ts";
import { jsonReplyScenarios } from "../mock/jsonReplyScenarios.ts";
import { realisticQaScenario } from "../mock/scenarios.ts";
import { buildReplayTimeline, replayTimelineBuilder } from "./replayTimeline.ts";
import { buildReplyRuns, replyRunsBuilder } from "./replyRuns.ts";
import { buildStepBoundaries, buildStepTimeline, shownWorkflowRows, stepTimelineBuilder } from "./stepTimeline.ts";
import { buildTranscript, transcriptBuilder } from "./transcript.ts";

const qa = realisticQaScenario.frames;
const worst = jsonReplyScenarios().worst.frames;
const [session, other] = ["wf-a", "wf-b"].map((id) => ({ ...realisticQaScenario.sessions[0], workflow_id: id }));
const started = qa.find((frame) => frame.event === "subagent_started").data;
const subagents = [{ subagentId: started.subagent_id, workflowId: started.workflow_id, label: "Researcher" }];

/* Two sessions' worth of growing prefixes in uneven batches, with the subagent observed halfway
   through the first: each step is [session, frames, subagents]. */
const steps = [];
for (const [s, frames] of [[session, [...qa, ...worst]], [other, worst]]) {
  for (let n = 1, batch = 1; n <= frames.length; n += batch, batch = (batch % 7) + 1) {
    steps.push([s, frames.slice(0, n), s === session && n > qa.length / 2 ? subagents : []]);
  }
}

/* `fold` gets the timeline the way the controller hands it over: folded too, so its entries
   keep their identity between steps. `rebuild` gets one built from scratch. */
function check(name, makeFold, rebuild) {
  it(`${name} matches a full rebuild at every step and leaves earlier results alone`, () => {
    const timeline = replayTimelineBuilder();
    const fold = makeFold();
    let previous = null;
    let reused = 0;
    for (const [s, frames, agents] of steps) {
      const folded = fold(timeline(s, frames, agents, "Agent"), frames);
      const expected = rebuild(buildReplayTimeline(s, frames, agents, "Agent"), frames);
      assert.deepEqual(folded, expected, `${name} differs at ${s.workflow_id}:${frames.length}`);
      if (previous) {
        assert.deepEqual(previous.value, previous.copy, `${name} changed an earlier result`);
        if (folded[0] != null && folded[0] === previous.value[0]) reused += 1;
      }
      previous = { value: folded, copy: structuredClone(folded) };
    }
    assert.ok(reused > steps.length / 2, `${name} rebuilt instead of folding (${reused} of ${steps.length} steps reused)`);
  });
}

describe("incremental projections", () => {
  check("replay timeline", () => (t) => t, (t) => t);
  check("transcript", () => { const fold = transcriptBuilder(); return (t) => fold(t); }, (t) => buildTranscript(t.filter((e) => e.role === "parent").map((e) => e.frame)));
  check("reply runs", () => { const fold = replyRunsBuilder(); return (t) => fold(t); }, (t) => buildReplyRuns(t));
  check("step timeline", () => { const fold = stepTimelineBuilder(); return (t) => fold(t).turns; }, (t) => buildStepTimeline(t).turns);
  check("usage timeline", () => { const fold = usageTimelineBuilder(); return (_, frames) => fold(frames); }, (_, frames) => buildUsageTimeline(frames));

  it("the timeline's rows read whole are its rows read one at a time, at every step", () => {
    const timeline = replayTimelineBuilder();
    const fold = stepTimelineBuilder();
    for (const input of [...steps.map(([s, frames, agents]) => timeline(s, frames, agents, "Agent")), ...interleaved]) {
      const { workflow } = fold(input);
      const oneByOne = { rowCount: workflow.rowCount, rowAt: workflow.rowAt };
      assert.deepEqual(workflow.rows(), Array.from({ length: workflow.rowCount }, (_, k) => workflow.rowAt(k)), `rows at ${input.length}`);
      assert.deepEqual(shownWorkflowRows(workflow), shownWorkflowRows(oneByOne), `shown rows at ${input.length}`);
    }
  });

  it("the replay timeline keeps folding when the same subagents arrive in another order", () => {
    const timeline = replayTimelineBuilder();
    const pair = [...subagents, { subagentId: "other", workflowId: "wf-other", label: "Writer" }];
    const first = timeline(session, qa, pair, "Agent");
    const second = timeline(session, qa, [...pair].reverse(), "Agent");
    assert.equal(second[0], first[0]);
  });

  it("the timeline's workflow rows and gaps match a full rebuild at every step", () => {
    const timeline = replayTimelineBuilder();
    const fold = stepTimelineBuilder();
    for (const input of [...steps.map(([s, frames, agents]) => timeline(s, frames, agents, "Agent")), ...interleaved, ...subagentsOutliveTurn]) {
      const { workflow } = fold(input);
      const expected = buildStepTimeline(input).workflow;
      assert.deepEqual(workflow.rows(), expected.rows(), `workflow rows at ${input.length}`);
      assert.deepEqual(workflow.gaps, expected.gaps, `gaps at ${input.length}`);
    }
  });

  it("step boundaries read off folded turns are every span edge, once, in order", () => {
    const timeline = replayTimelineBuilder();
    const fold = stepTimelineBuilder();
    for (const input of [...steps.map(([s, frames, agents]) => timeline(s, frames, agents, "Agent")), ...interleaved]) {
      const folded = fold(input);
      const edges = folded.turns.flatMap((turn) =>
        [...turn.spans, ...turn.subagentTurns.flatMap((sub) => sub.spans)].flatMap((span) => [span.startIndex, span.endIndex])
      );
      assert.deepEqual(buildStepBoundaries(folded), [...new Set(edges)].sort((a, b) => a - b), `boundaries at ${input.length}`);
    }
  });
});

const entry = (event, timestamp, turnNumber, data = {}) => ({
  frame: { event, data: { type: event, agent_id: "root", turn_number: turnNumber, timestamp, ...data } },
  workflowId: "root-workflow",
  role: "parent",
  label: "Agent"
});

/* A tool of turn 1 that ends after turn 2 has started, so a later frame rewrites an earlier
   turn's rows rather than the last turn's. */
const interleaved = (() => {
  const tool = (id) => ({ tool_id: id, tool_name: `tool ${id}`, tool_input: {} });
  const all = [
    entry("turn_started", 0, 1),
    entry("tool_start", 1, 1, tool("a")),
    entry("turn_started", 2, 2),
    entry("tool_start", 3, 2, tool("b")),
    entry("tool_end", 4, 1, tool("a")),
    entry("tool_end", 5, 2, tool("b")),
    entry("turn_started", 6, 3),
    entry("tool_start", 7, 3, tool("c"))
  ];
  return all.map((_, n) => all.slice(0, n + 1));
})();

/* Subagents started in turn 1 that end in turn 2: one stops, one is dispatched again. Their rows
   belong to turn 1, so a turn-2 frame has to rewrite it. */
const subagentsOutliveTurn = (() => {
  const sub = (id) => ({ subagent_id: id, workflow_id: `child-${id}`, agent_key: "researcher" });
  const all = [
    entry("turn_started", 0, 1),
    entry("subagent_started", 1, 1, sub("s1")),
    entry("subagent_started", 2, 1, sub("s2")),
    entry("turn_end", 3, 1),
    entry("turn_started", 4, 2),
    entry("subagent_stopped", 5, 2, sub("s1")),
    entry("subagent_started", 6, 2, sub("s2")),
    entry("turn_end", 7, 2)
  ];
  return all.map((_, n) => all.slice(0, n + 1));
})();
