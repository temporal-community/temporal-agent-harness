// ABOUTME: Asserts the two rules a log row's status chip obeys — when it appears, and what colour it
// is. It appears only when the row's label does not already carry the status, so every event kind
// whose status is one-to-one with its label suppresses it and the kinds whose status comes from the
// payload keep it. Its colour comes from statusKind(), which has to let the outcome answer before
// the actor does, or a subagent's ok and error come out the same colour. This is the check that
// fails when someone adds an event kind whose status stops being derivable, or reorders statusKind's
// branches back.
//   node ui/scripts/check-status-note.mjs

import assert from "node:assert/strict";
import "./libAlias.mjs";
import "./svelteLoader.mjs";

/* All three of these were hand-copied here until libAlias.mjs and svelteLoader.mjs
   landed — the stem table and statusNote() because plain node could not follow
   `$lib/`, statusKind() and the tone lookup because it could not read a .svelte
   file at all. A check that reimplements what it checks passes forever while the
   real thing rots, so they are imported now and the copies are gone. What is left
   below is fixture and claim: the label/status pairs the app can produce, and what
   should happen to each. */
const { statusNote } = await import("../src/lib/state/replayLog.ts");
const { statusKind } = await import("../src/lib/components/agent/TranscriptPanel.svelte");
const { STATUS_TONES } = await import("../src/lib/components/primitives/StatusChip.svelte");

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
  20,
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

/* The two rows that still draw a chip after the suppression above. These are
   the only statusKind() answers a reader can actually see, so they are the ones
   worth pinning. */
const ok = { actor: "subagent", tone: "done", status: "ok", label: "Subagent reply received" };
const failed = { actor: "subagent", tone: "error", status: "error", label: "Subagent reply received" };

assert.equal(statusKind(ok), "complete", "a subagent reply that succeeded reads as complete");
assert.equal(statusKind(failed), "error", "a subagent reply that failed reads as an error");

/* The regression this ordering fixes: `if (actor === "subagent") return
   "delegating"` used to sit above the outcome tests and swallow them, so ok,
   error, and still-running subagent rows all came out one colour. */
assert.notEqual(
  statusKind(ok),
  statusKind(failed),
  "ok and error on a subagent reply must not resolve to the same kind"
);

/* Kinds are only half the claim — the reader sees a colour, not a kind, and two
   distinct kinds can still be drawn in one hue. STATUS_TONES is StatusChip's own
   table, so this reads the mapping the chip will actually use. */
assert.notEqual(
  STATUS_TONES[statusKind(ok)],
  STATUS_TONES[statusKind(failed)],
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

/* Every kind statusKind() can return has to have a hue, or the chip renders
   undefined. Cheap to assert now that the real table is in hand, and impossible
   to assert while it was a two-entry copy. */
for (const kind of new Set(ROWS.map(([label, status]) => statusKind({ label, status })))) {
  assert.ok(STATUS_TONES[kind], `statusKind returned "${kind}", which StatusChip has no tone for`);
}

console.log(
  `check-status-note: ${ROWS.length} label/status pairs OK, statusKind ordering OK`
);
