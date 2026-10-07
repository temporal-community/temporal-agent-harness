import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import ApprovalDecisionPanel, { traceText } from "./ApprovalDecisionPanel.svelte";

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

const html = (decisions, props = {}) =>
  render(ApprovalDecisionPanel, { props: { decisions, ...props } }).body;

describe("the approval decision panel", () => {
  it("explains its cursor-aware empty state", () => {
    const body = html([]);
    assert.match(body, /No completed approval decisions/);
    assert.match(body, /auto_approval_evaluation_ended/);
  });

  /* The empty state a reader actually hits: the run HAS judged calls, the chat beside this
     pane shows the finished conversation, and the cursor is parked behind the evaluations.
     Saying "no completed approval decisions" there is true of the cursor and false of the
     run, and nothing else on screen closes that gap. */
  it("distinguishes an empty run from a cursor parked behind the decisions", () => {
    const body = html([], { ahead: 5 });
    assert.match(body, /5 approval decisions ahead of the replay cursor/);
    assert.match(body, /Jump to latest step/);
    assert.doesNotMatch(
      body,
      /No completed approval decisions/,
      "the run is not empty, so it must not say so"
    );
  });

  it("counts one decision in the singular", () => {
    const body = html([], { ahead: 1 });
    assert.match(body, /1 approval decision ahead/);
    assert.doesNotMatch(body, /1 approval decisions/);
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

    assert.equal((body.match(/<button[^>]*class="evaluation\b/g) ?? []).length, 2);
    assert.match(body, /aria-pressed="true"/);
    assert.match(body, /aria-pressed="false"/);
    assert.match(body, /delete_record/);
  });

  /* Each copy control sits in the same `.copyable` wrapper as the ID it copies, so the ID
     shown beside a button is the one it writes. */
  it("offers a named copy for each audit ID, beside that ID", () => {
    const body = stripComments(html([baseDecision]));
    for (const [id, label] of [
      ["tool-1", "Copy tool call ID"],
      ["e1", "Copy evaluation ID"],
      ["req-1", "Copy request ID"]
    ]) {
      assert.match(
        body,
        new RegExp(`class="copyable[^"]*">\\s*${id}\\s*<button[^>]*aria-label="${label}"`),
        label
      );
    }
    assert.match(body, /aria-label="Copy decision trace"/);
  });

  it("copies the decision trace as the evaluator, verdict and rationale", () => {
    const text = traceText(baseDecision);
    assert.match(text, /^tool: issue_refund$/m);
    assert.match(text, /^tool_id: tool-1$/m);
    assert.match(text, /^evaluator: jev_evaluator$/m);
    assert.match(text, /^outcome: Auto-approved$/m);
    assert.match(text, /^confidence: 95%$/m);
    assert.match(text, /^reason: The call satisfies the financial criteria\.$/m);
    assert.match(text, /^evaluation_id: e1$/m);
    assert.match(text, /^request_id: req-1$/m);
  });
});
