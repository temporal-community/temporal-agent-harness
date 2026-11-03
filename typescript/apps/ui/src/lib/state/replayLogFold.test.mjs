// The replay log folded a frame at a time, and cut at a cursor, must be the log rebuilt from
// scratch: same rows, same groups, at every length and every cursor.
//   npx vitest run src/lib/state/replayLogFold.test.mjs
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { jsonReplyScenarios } from "../mock/jsonReplyScenarios.ts";
import { realisticQaScenario } from "../mock/scenarios.ts";
import { buildReplayLog, replayLogAt, replayLogBuilder } from "./replayLog.ts";
import { buildReplayTimeline } from "./replayTimeline.ts";

/* Streamed replies, a subagent, approvals and handler errors, with one root frame dropped
   halfway so a history gap is in play too. */
const qa = realisticQaScenario.frames;
const frames = [
  ...qa.slice(0, qa.length >> 1),
  ...qa.slice((qa.length >> 1) + 1),
  ...jsonReplyScenarios().worst.frames
];

describe("replay log fold", () => {
  it("matches a full rebuild at every length, keeping untouched rows", () => {
    const fold = replayLogBuilder();
    let previous = null;
    for (let length = 1; length <= frames.length; length += 1) {
      const prefix = frames.slice(0, length);
      const folded = fold(prefix);
      assert.deepEqual(folded, buildReplayLog(prefix), `log differs after ${length} frames`);
      if (previous) {
        const kept = previous.rows.slice(0, -1);
        assert.ok(kept.every((row, i) => folded.rows[i] === row), `rows before the tail were rebuilt at ${length}`);
      }
      previous = folded;
    }
  });

  it("cuts to the same log a rebuild of the prefix gives, at every cursor", () => {
    for (const input of [frames, withSubagent, interleavedTurns]) {
      const full = buildReplayLog(input);
      for (let cursor = 1; cursor < input.length; cursor += 1) {
        assert.deepEqual(replayLogAt(full, input, cursor), buildReplayLog(input.slice(0, cursor)), `cut differs at ${cursor}`);
      }
    }
  });
});

/* The subagent observed, so its rows land in the parent's turns. */
const started = qa.find((frame) => frame.event === "subagent_started").data;
const withSubagent = buildReplayTimeline(realisticQaScenario.sessions[0], qa, [
  { subagentId: started.subagent_id, workflowId: started.workflow_id, label: "Researcher" }
], "Agent");

/* A turn-1 tool that ends after turn 2 has started, so the two turns' rows interleave, then a
   turn whose first row is a reply still streaming. */
const frame = (event, timestamp, turn_number, data = {}) => ({
  event,
  data: { type: event, turn_number, turn_id: `turn-${turn_number}`, timestamp, ...data }
});
const tool = (id) => ({ tool_id: id, tool_name: `tool ${id}`, tool_input: {} });
const interleavedTurns = [
  frame("turn_started", 0, 1),
  frame("tool_start", 1, 1, tool("a")),
  frame("turn_started", 2, 2),
  frame("reply_delta", 3, 2, { text: "Two " }),
  frame("reply_delta", 3, 2, { text: "streams" }),
  frame("tool_end", 4, 1, tool("a")),
  frame("tool_start", 5, 2, tool("b")),
  frame("turn_end", 6, 1),
  frame("tool_end", 7, 2, tool("b")),
  frame("turn_end", 8, 2),
  frame("reply_delta", 9, 3, { text: "Unannounced " }),
  frame("reply_delta", 9, 3, { text: "turn" })
];
