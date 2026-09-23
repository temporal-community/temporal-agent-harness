import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildApprovalDecisions } from "./approvalDecisionTree.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });

function entry(event, options = {}) {
  return {
    workflowId: options.workflowId ?? "wf-root",
    role: options.role ?? "parent",
    label: options.label ?? "Planner",
    frame: event
  };
}

function ended(overrides = {}) {
  return frame("auto_approval_evaluation_ended", {
    agent_id: "a1b2c3",
    turn_id: "turn-2",
    turn_number: 2,
    message_id: "msg-2",
    timestamp: 120,
    resume_offset: 9,
    tool_id: "tool-refund",
    tool_name: "issue_refund",
    evaluation_id: "evaluation-1",
    evaluator: "jev_evaluator",
    verdict: "approve",
    reason: "Auto-approved.",
    details: {
      model: "jev-fast",
      request_id: "req-1",
      verdict: "approve",
      confidence: 0.95,
      probabilities: { approve: 0.95, deny: 0.03, escalate: 0.02 },
      irreversible: 0.1,
      thresholds: {
        min_confidence: 0.8,
        escalate_if_irreversible_above: 0.5
      },
      criteria_set: "financial",
      criteria_version: 3
    },
    ...overrides
  });
}

describe("automatic approval decision trees", () => {
  it("rebuilds the successful policy path from the durable event", () => {
    const [decision] = buildApprovalDecisions([entry(ended())]);

    assert.equal(decision.key, "wf-root:evaluation-1");
    assert.equal(decision.toolName, "issue_refund");
    assert.equal(decision.verdict, "approve");
    assert.equal(decision.confidence, 0.95);
    assert.equal(decision.confidenceThreshold, 0.8);
    assert.equal(decision.probabilities.deny, 0.03);
    assert.equal(decision.criteriaSet, "financial");
    assert.equal(decision.criteriaVersion, 3);
    assert.deepEqual(
      decision.stages.map((stage) => stage.kind),
      ["confidence", "verdict", "irreversibility", "outcome"]
    );
    assert.equal(decision.stages[0].passed, true);
    assert.equal(decision.stages[2].passed, true);
  });

  it("stops at a failed confidence gate, before treating the raw label as a decision", () => {
    const [decision] = buildApprovalDecisions([
      entry(
        ended({
          verdict: "escalate",
          reason: "Below the 80% confidence needed to decide automatically.",
          details: {
            verdict: "deny",
            confidence: 0.61,
            probabilities: { approve: 0.15, deny: 0.61, escalate: 0.24 },
            irreversible: 0.9,
            thresholds: {
              min_confidence: 0.8,
              escalate_if_irreversible_above: 0.5
            }
          }
        })
      )
    ]);

    assert.equal(decision.evaluatorVerdict, "deny");
    assert.equal(decision.verdict, "escalate");
    assert.deepEqual(
      decision.stages.map((stage) => stage.kind),
      ["confidence", "outcome"]
    );
    assert.equal(decision.stages[0].passed, false);
  });

  it("does not apply the irreversibility gate to a denial", () => {
    const [decision] = buildApprovalDecisions([
      entry(
        ended({
          verdict: "deny",
          details: {
            verdict: "deny",
            confidence: 0.92,
            irreversible: 0.99,
            thresholds: {
              min_confidence: 0.8,
              escalate_if_irreversible_above: 0.5
            }
          }
        })
      )
    ]);

    assert.deepEqual(
      decision.stages.map((stage) => stage.kind),
      ["confidence", "verdict", "outcome"]
    );
    assert.equal(decision.stages[1].verdict, "deny");
  });

  it("keeps fallback evaluators useful when structured scores are absent", () => {
    const timeline = [
      entry(
        frame("auto_approval_evaluation_started", {
          evaluation_id: "ignored",
          tool_id: "tool-read",
          tool_name: "read",
          evaluator: "fallback"
        })
      ),
      entry(
        ended({
          evaluation_id: "evaluation-child",
          evaluator: "fallback",
          verdict: "escalate",
          details: { confidence: 4, probabilities: "not-a-record" }
        }),
        { workflowId: "wf-child", role: "subagent", label: "Researcher" }
      )
    ];

    const [decision] = buildApprovalDecisions(timeline);
    assert.equal(decision.key, "wf-child:evaluation-child");
    assert.equal(decision.agentLabel, "Researcher");
    assert.equal(decision.role, "subagent");
    assert.equal(decision.confidence, null);
    assert.deepEqual(
      decision.stages.map((stage) => stage.kind),
      ["verdict", "outcome"]
    );
  });
});
