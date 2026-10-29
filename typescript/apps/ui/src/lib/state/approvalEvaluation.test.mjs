// ABOUTME: Asserts that the automatic approval check reads as its OWN span, nested inside the
// approval gate rather than fused into it.
//
// This is the whole reason the harness publishes a start/end bracket around a custom approval
// fallback instead of one terminal event. An AI approver does a model round-trip on the critical
// path of every gated call, and before the bracket existed that time was invisible: the gate's
// single `requested -> resolved` bar charged it to "approval", alongside however long a human took
// to click. "Is our approval p99 the approver or the people?" was unanswerable from the UI.
//
// Two things are asserted because both can regress silently and neither shows in a screenshot
// until it is already wrong: (1) the evaluation is a separate span with its own duration; (2) in
// the EXCLUSIVE rollup the nested evaluation outranks the gate it sits inside (spanPriority), so
// its seconds are attributed to "approval check" and what is left under "approval" is the human's
// wait. Rank those the other way and the split silently collapses back into one number.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { aggregateSpans, buildStepTimeline } from "$lib/state/stepTimeline.ts";

let clock = 0;
const frame = (event, data) => ({
  event,
  data: { type: event, agent_id: "root", turn_number: 1, timestamp: clock, ...data }
});

/* One gated call: the gate opens at t=10, an approver spends 2s deciding and escalates, a
   person takes a further 28s. The gate is open 30s; only 2s of it is the approver. */
function escalatedGate() {
  const frames = [];
  const at = (ts, f) => {
    clock = ts;
    frames.push(f());
  };

  at(0, () => frame("turn_started", { user_message: "go" }));
  at(10, () => frame("tool_approval_requested", { tool_id: "t1", tool_name: "refund" }));
  at(10, () =>
    frame("auto_approval_evaluation_started", {
      tool_id: "t1",
      tool_name: "refund",
      evaluation_id: "e1",
      evaluator: "jev_evaluator"
    })
  );
  at(12, () =>
    frame("auto_approval_evaluation_ended", {
      tool_id: "t1",
      tool_name: "refund",
      evaluation_id: "e1",
      evaluator: "jev_evaluator",
      verdict: "escalate",
      reason: "confidence 61% < 80%",
      details: { confidence: 0.61 },
      applied: true
    })
  );
  at(40, () => frame("tool_approval_resolved", { tool_id: "t1", approved: true }));
  at(40, () => frame("tool_start", { tool_id: "t1", tool_name: "refund" }));
  at(41, () => frame("tool_end", { tool_id: "t1" }));

  return buildStepTimeline(frames);
}

describe("the automatic approval check as its own span", () => {
  it("times the approver apart from the gate it runs inside", () => {
    const [turn] = escalatedGate().turns;
    const evaluation = turn.spans.find((span) => span.kind === "evaluation");
    const approval = turn.spans.find((span) => span.kind === "approval");

    assert.ok(evaluation, "the evaluation bracket should produce a span of its own");
    assert.equal(evaluation.durationSeconds, 2);
    assert.match(evaluation.label, /jev_evaluator/);
    /* An escalate is not a failure — the approver correctly declined to decide — so it
       stays approval-toned rather than reading as an error. */
    assert.equal(evaluation.tone, "approval");
    assert.equal(evaluation.detail, "confidence 61% < 80%");

    // The gate spans the whole wait, evaluation included; the split is what separates them.
    assert.equal(approval.durationSeconds, 30);
  });

  it("pairs the bracket by evaluation_id, so two checks on one call stay distinct", () => {
    const frames = [];
    const at = (ts, f) => {
      clock = ts;
      frames.push(f());
    };
    const evaluation = (event, id, evaluator, extra = {}) =>
      frame(event, {
        tool_id: "t1",
        tool_name: "refund",
        evaluation_id: id,
        evaluator,
        ...extra
      });

    at(0, () => frame("turn_started", { user_message: "go" }));
    at(0, () => frame("tool_approval_requested", { tool_id: "t1", tool_name: "refund" }));
    /* A cascade over ONE gated call: a cheap check, then an expensive one. tool_id is the
       same for both, so only evaluation_id can keep them apart. */
    at(0, () => evaluation("auto_approval_evaluation_started", "e1", "cheap_check"));
    at(1, () =>
      evaluation("auto_approval_evaluation_ended", "e1", "cheap_check", {
        verdict: "escalate",
        reason: null,
        details: {},
        applied: true
      })
    );
    at(1, () => evaluation("auto_approval_evaluation_started", "e2", "jev_evaluator"));
    at(9, () =>
      evaluation("auto_approval_evaluation_ended", "e2", "jev_evaluator", {
        verdict: "approve",
        reason: "ok",
        details: {},
        applied: true
      })
    );
    at(9, () => frame("tool_approval_resolved", { tool_id: "t1", approved: true }));

    const [turn] = buildStepTimeline(frames).turns;
    const evaluations = turn.spans.filter((span) => span.kind === "evaluation");
    assert.equal(evaluations.length, 2, "one span per evaluation, not one per gated call");
    assert.deepEqual(
      evaluations.map((span) => span.durationSeconds).sort((a, b) => a - b),
      [1, 8]
    );
  });

  it("charges the nested check to itself in the exclusive rollup, not to the gate", () => {
    const totals = new Map(
      aggregateSpans(escalatedGate()).map((entry) => [entry.kind, entry.totalSeconds])
    );

    assert.equal(totals.get("evaluation"), 2);
    /* 30s of open gate minus the 2s the approver held it = the time a PERSON took. If
       spanPriority ever ranks the gate above the check nested in it, this becomes 30 and
       the whole split is silently undone. */
    assert.equal(totals.get("approval"), 28);
  });
});
