// ABOUTME: Asserts buildReplayMarkers reads markers off an already-built ReplayLog and keeps
// every field the scrub lane needs. It used to call buildReplayLog itself, so a signature
// change that drops index, tone, or label would still type-check while the lane went blank.
//   node ui/scripts/check-replay-markers.mjs

import assert from "node:assert/strict";
import "./libAlias.mjs";

const { buildReplayLog, buildReplayMarkers } = await import("../src/lib/state/replayLog.ts");

const meta = {
  agent_id: "agent",
  turn_id: "turn-1",
  turn_number: 1,
  timestamp: 1_700_000_000,
  resume_offset: 0
};

const log = buildReplayLog([
  {
    event: "turn_started",
    data: { ...meta, type: "turn_started", user_message: "hi" }
  },
  {
    event: "tool_approval_requested",
    data: {
      ...meta,
      type: "tool_approval_requested",
      tool_id: "call_1",
      tool_name: "search",
      tool_input: { q: "cats" }
    }
  }
]);

assert.equal(log.rows.length, 2, "the fixture must produce two rows so an unmarked one sits beside a marked one");
assert.equal(log.rows[0].marker, undefined, "turn_started is the unmarked neighbour");
assert.equal(log.rows[1].marker, "approval");

const markers = buildReplayMarkers(log);
assert.deepEqual(markers, [
  {
    id: "marker-2",
    index: 2,
    turnNumber: 1,
    tone: "approval",
    label: "approval requested"
  }
]);

console.log("check-replay-markers: marked row becomes a marker, unmarked row does not");
