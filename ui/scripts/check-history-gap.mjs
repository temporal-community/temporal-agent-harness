// ABOUTME: Asserts that a discontinuity in a session's history is named where it happens, and that
// nothing else is mistaken for one. A resume from an offset the workflow has already trimmed away
// (`_truncate_stream_for_handover` keeps a bounded tail across continue-as-new) is served from the
// oldest retained offset with no word about the hole, so a console that hydrates a stale frame
// cache and then attaches splices two windows together and presents the join as consecutive.
// Measured on a live session: `from_offset=2000` answered with a first `event_offset` of 4906, and
// 2,023 cached frames were spliced onto 2,044 new ones. The waterfall then drew the straddling turn
// as "no measured parent steps" — which is what a turn that genuinely ran none looks like.
//
//   node ui/scripts/check-history-gap.mjs

import assert from "node:assert/strict";
import "./libAlias.mjs";

const { HISTORY_GAP_NOTE, findHistoryGaps } = await import("../src/lib/state/historyGap.ts");
const { buildStepTimeline } = await import("../src/lib/state/stepTimeline.ts");
const { buildReplayLog } = await import("../src/lib/state/replayLog.ts");
const { realisticQaScenario } = await import("../src/lib/mock/scenarios.ts");

const ROOT = "c9105b";
const CHILD = `${ROOT}-093b70`;

let clock = 0;

/* Offsets are supplied rather than counted, because they are the whole subject: a fixture that
   numbered its own frames could not express the one shape under test. `resume_offset` rides along
   at `event_offset + 1` the way the merge emits it for a root event. */
function frame(event, offset, data = {}, agentId = ROOT, turnNumber = 1) {
  return {
    event,
    data: {
      type: event,
      agent_id: agentId,
      turn_number: turnNumber,
      turn_id: `${agentId}-t${turnNumber}`,
      timestamp: (clock += 1),
      event_offset: offset,
      resume_offset: offset + 1,
      ...data
    }
  };
}

const modelStart = (offset, turnNumber = 1, agentId = ROOT) =>
  frame("model_interaction_started", offset, { model: "gemini-3.5-flash" }, agentId, turnNumber);
const modelEnd = (offset, turnNumber = 1, agentId = ROOT) =>
  frame(
    "model_interaction_ended",
    offset,
    { model: "gemini-3.5-flash", usage: { total_tokens: 10 } },
    agentId,
    turnNumber
  );
const delta = (offset, text = "hi", turnNumber = 1, agentId = ROOT) =>
  frame("reply_delta", offset, { text }, agentId, turnNumber);

const turnsOf = (frames) => buildStepTimeline(frames).turns;
const gapTurns = (frames) => turnsOf(frames).filter((turn) => turn.historyGap).map((t) => t.turnNumber);
const gapRows = (frames) => buildReplayLog(frames).rows.filter((row) => row.gapBefore);

// --- the defect: a stale cache spliced onto a later window ------------------
// The exact shape observed live, scaled down. Everything up to offset 3 came out of the frame
// cache; the attach that followed could only be served from 4906, and the turn it lands in has an
// end frame whose start was trimmed away.
// Turn 2's `model_interaction_ended` fell in the hole along with turn 3 entirely and the start of
// turn 4, so the fixture carries both halves of an unpairable pair across the seam: an open start
// before it and an orphaned end after it. That is what makes the span assertions below mean
// something — with nothing open, a projection that paired across a seam would look correct.
{
  const spliced = [
    frame("turn_started", 0, { user_message: "count to a million" }),
    modelStart(1),
    modelEnd(2),
    frame("turn_end", 3),
    frame("turn_started", 4, { user_message: "keep going" }, ROOT, 2),
    modelStart(5, 2),
    delta(6, "one, two, ", 2),
    // ---- seam: offsets 7..4905 no longer exist -----------------------------
    delta(4906, "nine hundred thousand, ", 4),
    modelEnd(4907, 4),
    frame("turn_end", 4908, {}, ROOT, 4)
  ];
  const gaps = findHistoryGaps(spliced);

  /* Two assertions, deliberately not one: removing the detection outright and making it fire on
     every frame are the two ways this goes wrong, and each has to be caught by its own sentence or
     a mutation test cannot tell which happened. This one is only reachable if a seam is found. */
  assert.ok(
    gaps.has(7),
    "the seam is found, at the position of the frame that follows the jump"
  );
  assert.equal(gaps.size, 1, "and only there — one jump in the offsets is one seam, not several");

  assert.deepEqual(gapTurns(spliced), [4], "the waterfall marks the turn that straddles the seam");
  assert.deepEqual(
    turnsOf(spliced).filter((turn) => !turn.historyGap).map((turn) => turn.turnNumber),
    [1, 2],
    "and only that turn — the turns wholly on one side of it are whole"
  );

  const rows = gapRows(spliced);
  assert.equal(rows.length, 1, "the log names the seam once");
  assert.equal(rows[0].gapBefore, HISTORY_GAP_NOTE, "with the note, which is all that is known");
  assert.equal(rows[0].ordinal, 8, "on the first row after the jump, not the last one before it");

  /* The note must not invent a size. `event_offset` counts log entries, and how many turns or
     tool calls the 4,902 missing entries amounted to is not recoverable from the two numbers in
     hand — so any digit here would be a fabrication presented as a measurement. */
  assert.match(HISTORY_GAP_NOTE, /^— /, "the unknown marker is a leading em dash, as NO_THOUGHT_SUMMARY is");
  assert.ok(
    !/\d/.test(HISTORY_GAP_NOTE),
    `the note must not claim a count of what was lost: ${HISTORY_GAP_NOTE}`
  );

  /* The other half of the original symptom, and a regression guard in its own right. That
     `model_interaction_ended` at 4907 has no `model_interaction_started` to pair with, because the
     start was trimmed away with the rest of turn 4's opening. `closeOpenSpan` drops the orphan,
     which is correct and must stay correct: turn 2 has a model span still open, so a projection
     that reached for any open span instead of the keyed one would pair that start with this end and
     draw a single bar over the whole hole — reporting 4,900 missing events as latency. The seam is
     labelled now; it must never also be measured. */
  const allSpans = turnsOf(spliced).flatMap((turn) => [
    ...turn.spans,
    ...turn.subagentTurns.flatMap((sub) => sub.spans)
  ]);
  /* `startIndex`/`endIndex` are 1-based, so the frame after the seam sits at `position + 1`. */
  const seamIndex = 8;
  assert.deepEqual(
    allSpans.filter((span) => span.startIndex < seamIndex && span.endIndex >= seamIndex),
    [],
    "no span may be paired across the seam: a bar drawn over the hole measures what is missing"
  );

  const straddling = turnsOf(spliced).find((turn) => turn.turnNumber === 4);
  assert.deepEqual(straddling.spans, [], "an end frame orphaned by the seam invents no span");
  assert.equal(
    turnsOf(spliced).find((turn) => turn.turnNumber === 1).spans.length,
    1,
    "and turn 1's own model span, which did pair, is untouched"
  );

  /* Turn 2's start is unresolved in what this view holds, and says so rather than borrowing an end
     from the far side of the seam. */
  const orphanedStart = turnsOf(spliced).find((turn) => turn.turnNumber === 2).spans;
  assert.equal(orphanedStart.length, 1, "turn 2 keeps the span it opened");
  assert.equal(orphanedStart[0].ongoing, true, "and reports it unresolved rather than closed");
  assert.ok(
    orphanedStart[0].endIndex < seamIndex,
    "ending it no later than the last frame of its own that survived"
  );
}

// --- ordinary continuous frames produce nothing ----------------------------
{
  const continuous = [
    frame("turn_started", 0, { user_message: "hello" }),
    modelStart(1),
    delta(2),
    modelEnd(3),
    frame("turn_end", 4),
    frame("turn_started", 5, { user_message: "again" }, ROOT, 2),
    modelStart(6, 2),
    modelEnd(7, 2),
    frame("turn_end", 8, {}, ROOT, 2)
  ];

  assert.deepEqual([...findHistoryGaps(continuous)], [], "a dense run of offsets has no seam");
  assert.deepEqual(gapTurns(continuous), [], "so no turn in the waterfall claims one");
  assert.deepEqual(gapRows(continuous), [], "and no row in the log does");
}

// --- the documented degradation is not a gap -------------------------------
// A fresh tab attaching from 0 to an already-truncated stream gets a short scrollback whose
// contents are continuous. There is no hole INSIDE what it holds, so marking it would put a
// scary note on every session that has ever rolled over — which is most long-lived ones.
{
  const coldLoad = [
    delta(4906, "nine hundred thousand, ", 4),
    modelEnd(4907, 4),
    frame("turn_end", 4908, {}, ROOT, 4)
  ];

  assert.deepEqual(
    [...findHistoryGaps(coldLoad)],
    [],
    "a window that merely STARTS late is not discontinuous: nothing here precedes 4906"
  );
  assert.deepEqual(gapTurns(coldLoad), [], "no turn is flagged");
  assert.deepEqual(gapRows(coldLoad), [], "no row is flagged");

  /* And the honest cost of that, asserted rather than left implied: this view really is missing
     the head of the run and nothing says so. The seam is only visible from both sides. */
  assert.equal(
    turnsOf(coldLoad).find((turn) => turn.turnNumber === 4).spans.length,
    0,
    "its orphaned end frame still draws no span, so it is not silently mismeasured either"
  );
}

// --- frames outside the root's offset space must not manufacture one -------
// The failure mode that would make this worse than nothing. A detector firing on ordinary traffic
// gets ignored, and then the real seam is ignored with it.
{
  // A subagent's offsets are positions in that CHILD's own log and interleave with the root's at
  // unrelated values, so a run with children would read as nothing but gaps. The child here is
  // AHEAD of its parent — a researcher streaming hundreds of deltas while the parent published a
  // handful of frames — which is the direction that discriminates: a child behind the root is
  // absorbed by the high-water mark and would pass even with the filter gone.
  const withChild = [
    frame("turn_started", 5, { user_message: "delegate" }),
    frame("subagent_message_sent", 6, {
      subagent_id: "093b70",
      agent_key: "researcher",
      workflow_id: "wf-child",
      function: "task_ask",
      subagent_turn: 1,
      from_offset: 0
    }),
    modelStart(400, 1, CHILD),
    modelEnd(401, 1, CHILD),
    delta(402, "child says hi", 1, CHILD),
    frame("subagent_reply_received", 7, {
      subagent_id: "093b70",
      agent_key: "researcher",
      workflow_id: "wf-child",
      function: "task_ask",
      subagent_turn: 1,
      outcome: "ok"
    }),
    frame("turn_end", 8)
  ];
  assert.deepEqual(
    [...findHistoryGaps(withChild)],
    [],
    "a subagent's own offsets are a different sequence and must be ignored, not compared"
  );
  assert.deepEqual(gapTurns(withChild), [], "so a delegating turn is not flagged");

  // A frame the server synthesized reports the SYNTHESIZED sentinel rather than an offset, because
  // it was never read off a log. Confirmed live: those arrive as -1. It goes FIRST here, which is
  // the position that discriminates — mid-sequence the high-water mark absorbs it, but as the
  // opening frame a sentinel read as a position sets the mark to -1 and makes the next real
  // offset look like a jump from the start of the log.
  const synthesized = [
    frame("subagent_stream_unavailable", -1, {
      subagent_id: "093b70",
      workflow_id: "wf-child",
      reason: "Subagent stream could not be read; refresh to retry."
    }),
    delta(10),
    delta(11)
  ];
  assert.deepEqual([...findHistoryGaps(synthesized)], [], "the SYNTHESIZED sentinel is not an offset");

  // A client-side stream error carries no event envelope at all, and a server predating
  // `event_offset` (and every mock fixture) carries no such field.
  const offsetless = [
    delta(20),
    { event: "error", data: { kind: "timeout", message: "gave up", resume_offset: 21 } },
    { event: "reply_delta", data: { type: "reply_delta", agent_id: ROOT, turn_number: 1, turn_id: "t", timestamp: 1, resume_offset: 22, text: "x" } },
    delta(21)
  ];
  assert.deepEqual(
    [...findHistoryGaps(offsetless)],
    [],
    "a frame with no offset to read is skipped, not treated as offset zero"
  );

  // The trap worth its own case: the session-level frames at `turn: 0` LOOK like they might sit
  // outside the sequence, and they do not — a live session published turns 0, 5, 6 and 7 into one
  // dense run. Skipping them the way the display projections do would invent a gap at each.
  const withTurnZero = [
    delta(30),
    frame("operator_command_started", 31, {
      operator_command_id: "op-1",
      command_name: "status",
      command_label: "/status",
      arg: null
    }, ROOT, 0),
    frame("operator_command_completed", 32, {
      operator_command_id: "op-1",
      command_name: "status",
      command_label: "/status",
      arg: null,
      text: "idle"
    }, ROOT, 0),
    delta(33)
  ];
  assert.deepEqual(
    [...findHistoryGaps(withTurnZero)],
    [],
    "turn-0 frames share the root's offset space, so they must stay in the comparison"
  );

  // Repeats and backward steps are not seams, and — the part that needs the high-water mark
  // rather than the previous offset — neither is the frame AFTER one. A stray 4 between a 5 and a
  // 6 leaves every offset from 4 to 6 present, so there is no hole to report; comparing against
  // the last offset seen would have called that 6 a jump.
  assert.deepEqual(
    [...findHistoryGaps([delta(5), delta(5), delta(4), delta(6)])],
    [],
    "only a jump past everything in hand counts, so out-of-order arrival invents no seam"
  );
}

// --- and nothing fires on the fixture every other check reads --------------
// The mock has no `event_offset` at all, so this is the "absent is not zero" case at the scale the
// rest of the suite runs at: a gap claimed here would appear in the demo console on first load.
{
  assert.deepEqual(
    [...findHistoryGaps(realisticQaScenario.frames)],
    [],
    "realisticQaScenario is continuous and must read as such"
  );
  assert.deepEqual(gapTurns(realisticQaScenario.frames), [], "no turn in the demo is flagged");
  assert.deepEqual(gapRows(realisticQaScenario.frames), [], "no row in the demo is flagged");
  console.log(
    `  realisticQaScenario: ${realisticQaScenario.frames.length} frames, no seam claimed`
  );
}

console.log("check-history-gap: a spliced history is named at its seam, and only there");
