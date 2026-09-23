import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import ApprovalDecisionPanel from "./ApprovalDecisionPanel.svelte";

const baseDecision = {
  key: "wf-root:e1",
  evaluationId: "e1",
  workflowId: "wf-root",
  agentLabel: "Planner",
  role: "parent",
  turnNumber: 4,
  timestamp: 100,
  toolId: "tool-1",
  toolName: "issue_refund",
  evaluator: "jev_evaluator",
  verdict: "approve",
  evaluatorVerdict: "approve",
  reason: "The call satisfies the financial criteria.",
  confidence: 0.95,
  confidenceThreshold: 0.8,
  irreversible: 0.1,
  irreversibleThreshold: 0.5,
  probabilities: { approve: 0.95, deny: 0.03, escalate: 0.02 },
  criteriaSet: "financial",
  criteriaVersion: 3,
  model: "jev-fast",
  requestId: "req-1",
  stages: [
    { kind: "confidence", score: 0.95, threshold: 0.8, passed: true },
    {
      kind: "verdict",
      verdict: "approve",
      probabilities: { approve: 0.95, deny: 0.03, escalate: 0.02 }
    },
    { kind: "irreversibility", score: 0.1, threshold: 0.5, passed: true },
    { kind: "outcome", verdict: "approve" }
  ]
};

const html = (decisions) => render(ApprovalDecisionPanel, { props: { decisions } }).body;

describe("the approval decision panel", () => {
  it("explains its cursor-aware empty state", () => {
    const body = html([]);
    assert.match(body, /No completed approval decisions/);
    assert.match(body, /auto_approval_evaluation_ended/);
  });

  it("renders confidence, its threshold, and the final verdict without color alone", () => {
    const body = html([baseDecision]);
    assert.match(body, /Confidence gate/);
    assert.match(body, /95%/);
    assert.match(body, /80% bar/);
    assert.match(body, /aria-label="Verdict confidence"/);
    assert.match(body, /data-verdict="approve"/);
    assert.match(body, /Auto-approved/);
    assert.match(body, /The call satisfies the financial criteria/);
  });

  it("makes completed evaluations a real selection control", () => {
    const second = {
      ...baseDecision,
      key: "wf-root:e2",
      evaluationId: "e2",
      toolId: "tool-2",
      toolName: "delete_record",
      verdict: "deny",
      evaluatorVerdict: "deny",
      stages: [
        {
          kind: "verdict",
          verdict: "deny",
          probabilities: { approve: null, deny: null, escalate: null }
        },
        { kind: "outcome", verdict: "deny" }
      ]
    };
    const body = html([baseDecision, second]);

    assert.equal((body.match(/<button/g) ?? []).length, 2);
    assert.match(body, /aria-pressed="true"/);
    assert.match(body, /aria-pressed="false"/);
    assert.match(body, /delete_record/);
  });
});
