// ABOUTME: Asserts that a log row's status chip only appears when the row's label does not already
// carry it. The property that matters: every event kind whose status is one-to-one with its label
// suppresses the chip, and the kinds whose status comes from the payload rather than the event type
// keep it. This is the check that fails when someone adds an event kind whose status stops being
// derivable — a new `status:` in replayLog.ts with no matching row below.
//   node ui/scripts/check-status-note.mjs

import assert from "node:assert/strict";

/* ponytail: the stem table and statusNote() below are MIRRORED from
   ui/src/lib/state/replayLog.ts rather than imported, so they can drift from the
   real ones. Importing is not possible today because replayLog.ts pulls in
   $lib/cost/pricing as a value import, which plain node cannot resolve without an
   alias shim this repo does not have. Swap both for a real import the moment such
   a shim lands for some other reason. Same shortcut, same reason, as
   check-frame-key.mjs. */

// Kept in step with IMPLIED_STATUS_STEMS in ui/src/lib/state/replayLog.ts.
const IMPLIED_STATUS_STEMS = {
  running: ["start", "progress", "stream"],
  done: ["complet", "final"],
  complete: ["complet", "final"],
  idle: ["end"],
  dispatched: ["sent"],
  approved: ["grant"]
};

// Kept in step with statusNote() in ui/src/lib/state/replayLog.ts.
function statusNote(row) {
  const status = row.status?.trim();
  if (!status) return null;
  const label = row.label.toLowerCase();
  const value = status.toLowerCase();
  if (label.includes(value)) return null;
  if ((IMPLIED_STATUS_STEMS[value] ?? []).some((stem) => label.includes(stem))) return null;
  return status;
}

/* Every label/status pair rowFromFrame() can produce, read off the branches in
   ui/src/lib/state/replayLog.ts. `null` means the label already says it and the
   chip is suppressed; a string is the chip that survives. */
const ROWS = [
  // [label, status, expected]
  ["Operator command started", "running", null],
  ["Operator command completed", "completed", null],
  ["Operator command failed", "failed", null],
  ["Model started", "running", null],
  ["Model completed", "completed", null],
  ["Tool requested", "requested", null],
  ["Approval requested", "awaiting", "awaiting"],
  ["Approval granted", "approved", null],
  ["Approval denied", "denied", null],
  ["Tool started", "running", null],
  ["Tool progress", "running", null],
  ["Tool completed", "done", null],
  ["Tool failed", "failed", null],
  ["Subagent started", "running", null],
  ["Subagent message sent", "dispatched", null],
  /* The one status that is read off the payload rather than the event type
     (subagent_reply_received carries outcome: "ok" | "error"), so it is the one
     status a label genuinely cannot predict. */
  ["Subagent reply received", "ok", "ok"],
  ["Subagent reply received", "error", "error"],
  ["Subagent stopped", "stopped", null],
  ["Subagent stream unavailable", "degraded", "degraded"],
  ["Reply streaming", "streaming", null],
  ["Final reply", "complete", null],
  ["Turn ended", "idle", null]
];

for (const [label, status, expected] of ROWS) {
  assert.equal(
    statusNote({ label, status }),
    expected,
    expected === null
      ? `"${label}" already says "${status}", so the status chip should be suppressed`
      : `"${label}" does not say "${status}", so the status chip should survive`
  );
}

const suppressed = ROWS.filter(([, , expected]) => expected === null).length;
assert.equal(
  suppressed,
  18,
  "the count is part of the claim: if a row moved between suppressed and surviving, say so here"
);

/* The rows that carry no status at all — turn_started, message_queued,
   thought_summary, text_annotation, error — must not fall through to a chip
   labelled "undefined". */
assert.equal(
  statusNote({ label: "User message received" }),
  null,
  "a row with no status has no chip"
);
assert.equal(
  statusNote({ label: "Reasoning summary", status: "   " }),
  null,
  "a whitespace status is no status"
);

/* The default is open, so a status nobody has taught the table about is shown
   rather than silently dropped. This is what makes the table safe to land before
   every future status value is known. */
assert.equal(
  statusNote({ label: "Model started", status: "timeout" }),
  "timeout",
  "an unrecognized status should fall through and stay visible"
);

console.log(`check-status-note: ${ROWS.length} label/status pairs OK`);
