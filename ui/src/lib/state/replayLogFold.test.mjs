// The replay log folded a frame at a time, and cut at a cursor, must be the log rebuilt from
// scratch: same rows, same groups, at every length and every cursor.
//   npx vitest run src/lib/state/replayLogFold.test.mjs
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { jsonReplyScenarios } from "../mock/jsonReplyScenarios.ts";
import { realisticQaScenario } from "../mock/scenarios.ts";
import { buildReplayLog, replayLogAt, replayLogBuilder } from "./replayLog.ts";

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
    const full = buildReplayLog(frames);
    for (let cursor = 1; cursor < frames.length; cursor += 1) {
      assert.deepEqual(replayLogAt(full, frames, cursor), buildReplayLog(frames.slice(0, cursor)), `cut differs at ${cursor}`);
    }
  });
});
