// ABOUTME: Asserts the two rules a log row's status chip obeys — when it appears, and what colour it
// is. It appears only when the row's label does not already carry the status, so every event kind
// whose status is one-to-one with its label suppresses it and the kinds whose status comes from the
// payload keep it. Its colour comes from statusKind(), which has to let the outcome answer before
// the actor does, or a subagent's ok and error come out the same colour. This is the check that
// fails when someone adds an event kind whose status stops being derivable, or reorders statusKind's
// branches back.
//   node ui/scripts/check-status-note.mjs

import assert from "node:assert/strict";

/* ponytail: everything above the assertions is MIRRORED from the app rather than
   imported, so it can drift from the real thing. Two different reasons, neither
   fixable here. The stem table and statusNote() come from
   ui/src/lib/state/replayLog.ts, which plain node cannot import because it pulls
   in $lib/cost/pricing as a value import and there is no alias shim in this repo;
   swap those for a real import the moment such a shim lands for some other
   reason. statusKind() and the tone lookup come from Svelte components
   (TranscriptPanel.svelte, StatusChip.svelte) and cannot be imported at all
   without a Svelte compile step, which is the same wall check-frame-key.mjs hit
   and answered the same way. Lifting statusKind() into a plain .ts module would
   make it importable, but only once the pricing shim exists too. */

// Kept in step with IMPLIED_STATUS_STEMS in ui/src/lib/state/replayLog.ts.
const IMPLIED_STATUS_STEMS = {
  running: ["start", "progress", "stream"],
  done: ["complet", "final"],
  complete: ["complet", "final"],
  idle: ["end"],
  dispatched: ["sent"],
  approved: ["grant"],
  degraded: ["unavailable"]
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
  ["Subagent stream unavailable", "degraded", null],
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
  19,
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

/* --- statusKind: the colour the surviving chip is drawn in ---------------- */

// Kept in step with statusKind() in ui/src/lib/components/agent/TranscriptPanel.svelte.
function statusKind(row) {
  const status = row.status?.toLowerCase() ?? "";
  if (row.tone === "error" || row.actor === "error" || status.includes("fail")) {
    return "error";
  }
  if (row.actor === "approval" || status.includes("approval") || status.includes("await")) {
    return "approval";
  }
  if (row.actor === "operator") return "queued";
  if (row.actor === "tool" || status.includes("tool")) return "tool";
  if (row.actor === "model") return "model";
  if (row.actor === "reasoning") return "reasoning";
  if (row.actor === "queue" || status.includes("queue")) return "queued";
  if (
    row.tone === "done" ||
    status.includes("done") ||
    status.includes("complete") ||
    status.includes("approved")
  ) {
    return "complete";
  }
  if (status.includes("running") || status.includes("streaming")) return "thinking";
  if (row.actor === "subagent") return "delegating";
  return "idle";
}

/* The three rows that still draw a chip after the suppression above. These are
   the only statusKind() answers a reader can actually see, so they are the ones
   worth pinning. */
const ok = { actor: "subagent", tone: "done", status: "ok", label: "Subagent reply received" };
const failed = { actor: "subagent", tone: "error", status: "error", label: "Subagent reply received" };
const awaiting = { actor: "approval", tone: "approval", status: "awaiting", label: "Approval requested" };

assert.equal(statusKind(ok), "complete", "a subagent reply that succeeded reads as complete");
assert.equal(statusKind(failed), "error", "a subagent reply that failed reads as an error");
assert.equal(statusKind(awaiting), "approval", "an approval still waiting reads as an approval");

/* The regression this ordering fixes: `if (actor === "subagent") return
   "delegating"` used to sit above the outcome tests and swallow them, so ok,
   error, and still-running subagent rows all came out one colour. */
assert.notEqual(
  statusKind(ok),
  statusKind(failed),
  "ok and error on a subagent reply must not resolve to the same kind"
);

/* Kinds are only half the claim — the reader sees a colour, not a kind. Mirrors
   the two entries of STATUS_TONES in ui/src/lib/components/primitives/StatusChip.svelte. */
const TONE_OF = { complete: "success", error: "error" };
assert.notEqual(
  TONE_OF[statusKind(ok)],
  TONE_OF[statusKind(failed)],
  "ok and error on a subagent reply must not render in the same hue"
);

/* Delegation is still the answer for a subagent row with nothing sharper to
   say, which is what makes it safe as a fallback rather than a headline. */
assert.equal(
  statusKind({ actor: "subagent", tone: "tool", status: "dispatched", label: "Subagent message sent" }),
  "delegating",
  "a subagent row with no outcome yet still reads as delegating"
);
assert.equal(
  statusKind({ actor: "subagent", tone: "agent", status: "running", label: "Subagent started" }),
  "thinking",
  "a subagent row that is still running reads as in flight, not as delegation"
);

/* Error wins over everything, including the actor tests above it. */
for (const row of [
  { actor: "subagent", tone: "error", status: "degraded", label: "Subagent stream unavailable" },
  { actor: "tool", tone: "error", status: "failed", label: "Tool failed" },
  { actor: "model", tone: "error", status: "failed", label: "Model failed" }
]) {
  assert.equal(statusKind(row), "error", `${row.label} must resolve to error before anything else`);
}

console.log(
  `check-status-note: ${ROWS.length} label/status pairs OK, statusKind ordering OK`
);
