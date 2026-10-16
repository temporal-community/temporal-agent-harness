// ABOUTME: Asserts that the frames attributed to the session's ROOT agent are the root's own and
// nobody else's, at the one place that decides it (`role` in buildReplayTimeline) and at both consumers
// that mis-render when it is wrong. A turn number counts the turns of ONE agent, so every
// subagent's first turn is also turn 1: a child's `turn_started` credited to the root puts a second
// "turn 1" chapter on the replay bar — which throws Svelte's each_key_duplicate on every reactive
// flush, since the lane keys chapters by turn number — plus a second "Turn 1" row in the chat, and
// a second turn-1 reply bubble beside the root's. The condition is a stream that
// opened past the parent's `subagent_started`, so the children are unannounced: real frames off
// /api/attach?from_offset=8 look exactly like the fixtures below.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { realisticQaScenario } from "../mock/scenarios.ts";
import { turnMarkersOf } from "./agentRun.svelte.ts";
import { buildReplayTimeline } from "./replayTimeline.ts";
import { buildTranscript } from "./transcript.ts";

const session = {
  workflow_id: "wf-root",
  created_at: 0,
  label: "Root",
  agent_workflow_type: "test"
};

function replayTimeline(frames) {
  return buildReplayTimeline(session, frames, [], "Root");
}

/* What that line used to say, kept only so the assertions below can show it is what produced
   the duplicates. Rootness inferred from never having seen the child's `subagent_started` — a
   different frame, on the parent's log, which a stream opened past its offset never carries, so
   `announced` here is the set of children the client happened to be told about. */
function replayTimelineByObservation(frames, announced = []) {
  return frames.map((frame) => ({
    role: announced.includes(frame.data.agent_id) ? "subagent" : "parent",
    frame
  }));
}


// Mirrors `chatTranscript`.
function chatTranscriptOf(timeline) {
  return buildTranscript(
    timeline.filter((entry) => entry.role === "parent").map((entry) => entry.frame)
  );
}

const keysOf = (markers) => markers.map((marker) => marker.turnNumber);
const unique = (keys) => keys.length === new Set(keys).size;
const turnRowsOf = (transcript) =>
  transcript.filter((item) => item.kind === "user").map((item) => item.turnNumber);
const replyTextsOf = (transcript, turnNumber) =>
  transcript
    .filter((item) => item.kind === "agent" && item.turnNumber === turnNumber)
    .map((item) => item.text);

let seq = 0;
/* The message's admission — the frame that carries what was sent, and the one the chat opens
   a user row on. Its `turn_started` is a separate, pure bracket. */
function accepted(agentId, turnNumber, text = "go") {
  return {
    event: "message_accepted",
    data: {
      type: "message_accepted",
      agent_id: agentId,
      turn_number: turnNumber,
      turn_id: `${agentId}-t${turnNumber}`,
      message_id: `${agentId}-m${turnNumber}`,
      handler: "ask",
      payload: { text },
      disposition: "opened",
      timestamp: ++seq
    }
  };
}
function turnStarted(agentId, turnNumber) {
  return {
    event: "turn_started",
    data: {
      type: "turn_started",
      agent_id: agentId,
      turn_number: turnNumber,
      turn_id: `${agentId}-t${turnNumber}`,
      message_id: null,
      timestamp: ++seq
    }
  };
}
function replyDelta(agentId, turnNumber, text) {
  return {
    event: "reply_delta",
    data: {
      type: "reply_delta",
      agent_id: agentId,
      turn_number: turnNumber,
      turn_id: `${agentId}-t${turnNumber}`,
      message_id: `${agentId}-m${turnNumber}`,
      text,
      timestamp: ++seq
    }
  };
}

// Verbatim shape of a real DeepResearchPlanner attach from an offset past the four
// `subagent_started` frames: nothing announced the children, and both are on their own turn 1
// alongside the root's turn 1.
const unannouncedChildren = [
  accepted("de539b", 1, "Research the thing"),
  turnStarted("de539b", 1),
  replyDelta("de539b", 1, "Dispatching researchers."),
  accepted("de539b-093b70", 1, "Research subtopic A"),
  turnStarted("de539b-093b70", 1),
  replyDelta("de539b-093b70", 1, "Subtopic A findings."),
  accepted("de539b-80e175", 1, "Research subtopic B"),
  turnStarted("de539b-80e175", 1),
  replyDelta("de539b-80e175", 1, "Subtopic B findings.")
];

describe("root-only attribution", () => {
  // --- the replay bar's chapters ---------------------------------------------
  it("gives the replay bar one chapter, the root's own", () => {
    const wasBroken = turnMarkersOf(replayTimelineByObservation(unannouncedChildren));
    assert.deepEqual(keysOf(wasBroken), [1, 1, 1], "the old rule admitted both children's turn 1");
    assert.ok(!unique(keysOf(wasBroken)), "which is the each_key_duplicate the lane threw");

    const markers = turnMarkersOf(replayTimeline(unannouncedChildren));
    assert.deepEqual(keysOf(markers), [1], "one chapter, the root's own turn 1");
    assert.ok(unique(keysOf(markers)), "a chapter key must be unique");
  });

  // --- the chat transcript ---------------------------------------------------
  // Same defect, different symptom: `buildTranscript` opens a user row per `message_accepted`
  // and labels every reply with its turn number, so a child credited to the root both adds a
  // bogus "Turn 1" row and puts its reply beside the root's as a second turn-1 bubble.
  it("gives the chat one turn row, holding only what the root said", () => {
    const wasBroken = chatTranscriptOf(replayTimelineByObservation(unannouncedChildren));
    assert.deepEqual(turnRowsOf(wasBroken), [1, 1, 1], "the old rule showed three Turn 1 rows");
    assert.deepEqual(
      replyTextsOf(wasBroken, 1),
      ["Dispatching researchers.", "Subtopic A findings.", "Subtopic B findings."],
      "and showed both children's replies as turn-1 bubbles of the root"
    );

    const transcript = chatTranscriptOf(replayTimeline(unannouncedChildren));
    assert.deepEqual(turnRowsOf(transcript), [1], "one turn row for the root's one turn");
    assert.ok(unique(turnRowsOf(transcript)), "a turn is shown once");
    assert.deepEqual(
      replyTextsOf(transcript, 1),
      ["Dispatching researchers."],
      "and the root's bubble is only what the root said"
    );
  });

  // --- the root's own turns are all still there -------------------------------
  // The fix must not buy uniqueness by dropping turns: an empty lane is worse than a duplicated
  // one, because nothing on screen says the turns went missing.
  it("keeps every one of the root's own turns", () => {
    const frames = [
      accepted("de539b", 1),
      turnStarted("de539b", 1),
      accepted("de539b-093b70", 1),
      turnStarted("de539b-093b70", 1),
      accepted("de539b-80e175", 1),
      turnStarted("de539b-80e175", 1),
      accepted("de539b", 2),
      turnStarted("de539b", 2),
      accepted("de539b-093b70", 2),
      turnStarted("de539b-093b70", 2),
      accepted("de539b", 3),
      turnStarted("de539b", 3)
    ];
    const markers = turnMarkersOf(replayTimeline(frames));

    assert.deepEqual(keysOf(markers), [1, 2, 3], "every root turn keeps its chapter");
    assert.deepEqual(
      markers.map((marker) => marker.index),
      [0, 6, 10],
      "and each chapter starts at the message that opened it"
    );
    assert.deepEqual(
      turnRowsOf(chatTranscriptOf(replayTimeline(frames))),
      [1, 2, 3],
      "and the chat shows those three turns, once each"
    );
  });

  // --- announced subagents, and deeper descendants ---------------------------
  // `role` was only ever right when the child HAD been announced, so the two rules must agree
  // there; and rootness is the id having one segment, not having exactly two, so a grandchild is
  // no more the root than its parent is.
  it("agrees with the old rule once every child is announced, grandchildren included", () => {
    const frames = [
      turnStarted("de539b", 7),
      turnStarted("de539b-093b70", 1),
      turnStarted("de539b-093b70-a1b2c3", 1)
    ];
    const announced = ["de539b-093b70", "de539b-093b70-a1b2c3"];

    assert.deepEqual(keysOf(turnMarkersOf(replayTimeline(frames))), [7], "only the root's turn 7");
    assert.deepEqual(
      turnMarkersOf(replayTimeline(frames)),
      turnMarkersOf(replayTimelineByObservation(frames, announced)),
      "the two rules agree once every child is announced"
    );
  });

  // --- the fixture every other check reads -----------------------------------
  // The sections above prove the rule on fixtures written here, which is the wrong place to prove it
  // about `realisticQaScenario`: turnNavigation.test.mjs and replayHotkeys.test.mjs both drive
  // the whole transport off that scenario's markers and assert only that there are `>= 3` of them,
  // so a duplicated chapter in the fixture satisfies both while the lane it feeds throws
  // each_key_duplicate on every flush. It happened: the mock's ids were the label-style `qa-root`
  // and `qa-root-search`, `search` is six characters that are not six hex digits, so the child read
  // as a second root and its turn 1 landed on the bar beside the root's — markers came out
  // `1,2,3,4,5,6,7,1,8,9,10,11,12,13`.
  //
  // So the property is asserted here, once, on the shared fixture rather than in either consumer:
  // the keys the lane will use must be strictly increasing, which is uniqueness plus the order a
  // reader scrubs in. Uniqueness alone would pass a bar whose chapters run backwards.
  it("holds on realisticQaScenario, the fixture every other check reads", () => {
    const markers = turnMarkersOf(replayTimeline(realisticQaScenario.frames));
    const keys = keysOf(markers);

    assert.ok(keys.length >= 3, `the scenario must carry turns at all (saw ${keys.length})`);
    assert.ok(
      unique(keys),
      `realisticQaScenario produces a duplicate turn marker — the each_key_duplicate case, in the ` +
        `fixture the transport checks read: ${keys.join(",")}. A subagent's turn is being credited ` +
        `to the root, which means an agent_id in the mock does not carry the documented trailing ` +
        `hex segment (AgentId in harness/agent_protocol/agent_interface.py). Conform the id; do ` +
        `not loosen isRootAgentEvent to admit it.`
    );
    assert.deepEqual(
      keys,
      [...keys].sort((a, b) => a - b),
      `the root's turn numbers must climb with the timeline, since the replay bar lays its chapters ` +
        `out in frame order: ${keys.join(",")}`
    );
    assert.deepEqual(
      markers.map((marker) => marker.index),
      [...markers.map((marker) => marker.index)].sort((a, b) => a - b),
      "and each chapter must sit later in the timeline than the one before it"
    );
  });
});
