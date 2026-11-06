// The transcript cut at the replay cursor must be the transcript rebuilt from the frames before
// it, wherever the cursor goes and whichever way it moves, and a step must cost the items it
// changes rather than the run: one frame changes at most one item, so every other item has to
// come back as the same object, read without touching a frame.
//   npx vitest run src/lib/state/transcriptAt.test.mjs
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { jsonReplyScenarios } from "../mock/jsonReplyScenarios.ts";
import { scaleScenario } from "../mock/scaleScenarios.ts";
import { realisticQaScenario } from "../mock/scenarios.ts";
import { buildReplayTimeline } from "./replayTimeline.ts";
import { buildTranscript, transcriptBuilder } from "./transcript.ts";

const frame = (event, timestamp, data = {}) => ({
  event,
  data: { type: event, turn_number: 1, turn_id: "turn-1", timestamp, ...data }
});
const tool = (id) => ({ tool_id: id, tool_name: `tool ${id}`, tool_input: { id } });

/* Two handlers streaming under one turn at once, a reply that ends without a delta, citations
   arriving mid-reply, a queued message, approvals settled by an evaluator, a superseded and a
   failed evaluation, a tool id a later turn uses again, and a reply changed after it ended. */
const interleavedFrames = [
  frame("message_accepted", 1, { message_id: "m1", handler: "ask", payload: { text: "first" } }),
  frame("turn_started", 1),
  frame("message_accepted", 2, { message_id: "m2", handler: "ask", payload: { text: "joins" } }),
  frame("reply_delta", 3, { message_id: "m1", text: "One " }),
  frame("reply_delta", 3, { message_id: "m2", text: '{"two": ' }),
  frame("tool_requested", 4, tool("t1")),
  frame("text_annotation", 4, { message_id: "m1", delta: { annotations: [{ type: "file_citation", file_id: "f", filename: "a.md", index: 0 }] } }),
  frame("auto_approval_evaluation_started", 5, { ...tool("t1"), evaluator: "policy" }),
  frame("reply_delta", 5, { message_id: "m1", text: "more" }),
  frame("auto_approval_evaluation_superseded", 6, { ...tool("t1"), evaluator: "policy" }),
  frame("tool_approval_resolved", 6, { ...tool("t1"), approved: true }),
  frame("tool_requested", 6, tool("t2")),
  frame("auto_approval_evaluation_started", 7, { ...tool("t2"), evaluator: "policy" }),
  frame("auto_approval_evaluation_error", 7, { ...tool("t2"), evaluator: "policy", message: "boom" }),
  frame("tool_start", 8, tool("t1")),
  frame("tool_progress_delta", 8, { ...tool("t1"), progress_delta: "half" }),
  frame("message_accepted", 9, { message_id: "m3", handler: "ask", payload: { text: "queued" } }),
  frame("message_handler_error", 9, { message_id: "m2", message: "failed" }),
  frame("tool_end", 10, { ...tool("t1"), tool_output: "ok" }),
  frame("message_handler_end", 10, { message_id: "m1", output: { text: "One more" } }),
  frame("tool_error", 11, { ...tool("t2"), message: "denied by policy" }),
  frame("turn_end", 11),
  frame("turn_started", 12, { turn_number: 2, turn_id: "turn-2" }),
  frame("message_handler_end", 13, { message_id: "m3", turn_number: 2, turn_id: "turn-2", output: { text: "No deltas" } }),
  frame("tool_requested", 13, { ...tool("t1"), turn_number: 2, turn_id: "turn-2" }),
  frame("tool_end", 14, { ...tool("t1"), turn_number: 2, turn_id: "turn-2", tool_output: "again" }),
  frame("message_handler_error", 14, { message_id: "m1", message: "late failure" }),
  frame("turn_end", 14, { turn_number: 2, turn_id: "turn-2" })
];

/* Declared prose, so m2's JSON-looking stream still reads as prose: only its message_accepted says so. */
const proseAsk = [
  {
    name: "ask",
    parameters: { type: "object", properties: { text: { type: "string" } } },
    output: { type: "object", properties: { text: { type: "string" } } }
  }
];

const qaStarted = realisticQaScenario.frames.find((f) => f.event === "subagent_started").data;
const timelineOf = (scenario, subagents = []) =>
  buildReplayTimeline(
    { ...(scenario.sessions?.[0] ?? realisticQaScenario.sessions[0]), workflow_id: "wf-root" },
    scenario.frames,
    subagents,
    "Agent"
  );
const scenarios = {
  "realistic QA, its subagent observed": [
    timelineOf(realisticQaScenario, [{ subagentId: qaStarted.subagent_id, workflowId: qaStarted.workflow_id, label: "Researcher" }])
  ],
  "JSON replies, worst case": [timelineOf(jsonReplyScenarios().worst), jsonReplyScenarios().worst.agentInterface],
  "three approvals": [timelineOf(scaleScenario("approvals3"))],
  "an evaluator burst": [timelineOf(scaleScenario("burst10"))],
  subagents: [timelineOf(scaleScenario("subagents"))],
  "steering claimed and dropped": [timelineOf(scaleScenario("steering"))],
  "replies and tools interleaved": [interleavedFrames, proseAsk]
};

/* Forward to the end, back to the start, then a zigzag and long jumps both ways. */
function cursorWalk(length) {
  const walk = [];
  for (let c = 0; c <= length; c += 1) walk.push(c);
  for (let c = length; c >= 0; c -= 1) walk.push(c);
  for (let c = 1; c < length; c += 2) walk.push(c + 1, c - 1, c);
  for (let k = 0; k < 40; k += 1) walk.push((k * 7919) % (length + 1));
  return walk;
}

const changedBetween = (a, b) => {
  let changed = Math.abs(a.length - b.length);
  for (let j = 0; j < Math.min(a.length, b.length); j += 1) if (a[j] !== b[j]) changed += 1;
  return changed;
};

describe("the transcript at the replay cursor", () => {
  for (const [name, [timeline, agentInterface]] of Object.entries(scenarios)) {
    it(`${name}: is the visible slice rebuilt, at every cursor, with one item changing per step`, () => {
      const fold = transcriptBuilder();
      const full = fold(timeline, agentInterface);
      let previous = null;
      for (const cursor of cursorWalk(timeline.length)) {
        const cut = fold.at(cursor);
        assert.deepEqual(cut, buildTranscript(timeline.slice(0, cursor), agentInterface), `differs at ${cursor}`);
        if (previous && Math.abs(previous.cursor - cursor) === 1) {
          assert.ok(changedBetween(previous.cut, cut) <= 1, `${changedBetween(previous.cut, cut)} items changed between ${previous.cursor} and ${cursor}`);
        }
        if (previous?.cursor === cursor) assert.equal(cut, previous.cut, `the same cut at ${cursor} is the same array`);
        previous = { cursor, cut };
      }
      const end = fold.at(timeline.length);
      end.forEach((item, j) => assert.equal(item, full[j], `item ${j} at the end is the full transcript's own`));
    });
  }

  it("follows the full transcript while it folds forward, with the cursor parked", () => {
    const fold = transcriptBuilder();
    const parked = 9;
    let previous = null;
    for (let n = parked; n <= interleavedFrames.length; n += 1) {
      fold(interleavedFrames.slice(0, n));
      const cut = fold.at(parked);
      assert.deepEqual(cut, buildTranscript(interleavedFrames.slice(0, parked)));
      /* An item settled at the cursor that a later frame changes is refolded once, from its own frames. */
      if (previous) {
        const changed = changedBetween(previous, cut);
        assert.ok(changed <= 1, `${changed} items changed when frame ${n} arrived after the cursor`);
        if (changed === 0) assert.equal(cut, previous, `frame ${n} arriving after the cursor keeps the cut`);
      }
      previous = cut;
    }
  });
});

/* `turns` turns of the same shape: a message, a long streamed reply interleaved with tools. */
function run(turns) {
  const frames = [];
  for (let n = 1; n <= turns; n += 1) {
    const at = { turn_number: n, turn_id: `turn-${n}` };
    const ids = { ...at, message_id: `m${n}` };
    frames.push(frame("message_accepted", n, { ...ids, handler: "ask", payload: { text: `ask ${n}` } }));
    frames.push(frame("turn_started", n, at));
    for (let k = 0; k < 6; k += 1) {
      frames.push(frame("tool_requested", n, { ...at, ...tool(`${n}-${k}`) }));
      frames.push(frame("reply_delta", n, { ...ids, text: `chunk ${k} ` }));
      frames.push(frame("tool_end", n, { ...at, ...tool(`${n}-${k}`), tool_output: "ok" }));
    }
    frames.push(frame("message_handler_end", n, { ...ids, output: { text: `reply ${n}` } }));
    frames.push(frame("turn_end", n, at));
  }
  return frames;
}

/* Every frame the cut reads goes through this getter. */
function counted(frames) {
  const reads = { count: 0 };
  const timeline = frames.map((f) => ({
    role: "parent",
    get frame() {
      reads.count += 1;
      return f;
    }
  }));
  return { timeline, reads };
}

/* The most frames read and items changed by any one step across the run's last two turns. */
function stepCost(turns) {
  const { timeline, reads } = counted(run(turns));
  const fold = transcriptBuilder();
  fold(timeline);
  const perTurn = timeline.length / turns;
  const from = timeline.length - 2 * perTurn;
  let frames = 0;
  let items = 0;
  let previous = fold.at(timeline.length);
  for (const cursor of [...Array.from({ length: 2 * perTurn }, (_, k) => timeline.length - 1 - k), ...Array.from({ length: 2 * perTurn }, (_, k) => from + 1 + k)]) {
    reads.count = 0;
    const cut = fold.at(cursor);
    frames = Math.max(frames, reads.count);
    items = Math.max(items, changedBetween(previous, cut));
    previous = cut;
  }
  return { frames, items, perTurn };
}

describe("a cursor step's cost", () => {
  it("is the items it changes and their own frames, however long the run", () => {
    const short = stepCost(20);
    const long = stepCost(2000);
    assert.deepEqual(long, short, "a step costs more in a longer run");
    assert.ok(short.items <= 1, `a step changed ${short.items} items`);
    assert.ok(short.frames <= short.perTurn, `a step read ${short.frames} frames, more than one turn's ${short.perTurn}`);
  });
});
