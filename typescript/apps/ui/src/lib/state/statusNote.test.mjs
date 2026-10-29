// ABOUTME: Asserts when a log row's status word appears: only when the row's label does not already
// carry the status, so every event kind whose status is one-to-one with its label suppresses it and
// the kinds whose status comes from the payload keep it. This is the check that fails when someone
// adds an event kind whose status stops being derivable.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

/* The stem table and statusNote() were hand-copied here for as long as this ran as a
   bare node script, because node could not follow `$lib/`. A test that reimplements
   what it tests passes forever while the real thing rots, so it is imported now and
   the copy is gone. What is left below is fixture and claim: the label/status pairs
   the app can produce, and what should happen to each. */
import { statusNote } from "./replayLog.ts";

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
  ["Approval requested", "awaiting", null],
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
  ["Subagent stream unavailable", "degraded", null],
  ["Reply streaming", "streaming", null],
  ["Final reply", "complete", null],
  ["Turn ended", "idle", null]
];

describe("statusNote", () => {
  it("shows a chip only when the label does not already say the status", () => {
    for (const [label, status, expected] of ROWS) {
      assert.equal(
        statusNote({ label, status }),
        expected,
        expected === null
          ? `"${label}" already says "${status}", so the status chip should be suppressed`
          : `"${label}" does not say "${status}", so the status chip should survive`
      );
    }

    /* The pair count is the coverage claim this file used to print on the way out:
       if a label/status pair is added or dropped, say so here. */
    assert.equal(ROWS.length, 22, "every label/status pair the app can produce is swept above");

    const suppressed = ROWS.filter(([, , expected]) => expected === null).length;
    assert.equal(
      suppressed,
      20,
      "the count is part of the claim: if a row moved between suppressed and surviving, say so here"
    );
  });

  /* The rows that carry no status at all — turn_started, message_queued,
     thought_summary, text_annotation, error — must not fall through to a chip
     labelled "undefined". */
  it("has no chip for a row that carries no status", () => {
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
  });

  /* The default is open, so a status nobody has taught the table about is shown
     rather than silently dropped. This is what makes the table safe to land before
     every future status value is known. */
  it("keeps a status nobody has taught the table about", () => {
    assert.equal(
      statusNote({ label: "Model started", status: "timeout" }),
      "timeout",
      "an unrecognized status should fall through and stay visible"
    );
  });
});
