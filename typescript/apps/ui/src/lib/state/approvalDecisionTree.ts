import type { JsonRecord } from "$lib/api/types";
import type { ReplayTimelineEntry } from "$lib/state/replayTimeline";

export type ApprovalVerdict = "approve" | "deny" | "escalate";

export interface ApprovalProbabilities {
  approve: number | null;
  deny: number | null;
  escalate: number | null;
}

export type ApprovalDecisionStage =
  | {
      kind: "confidence";
      score: number;
      threshold: number | null;
      passed: boolean | null;
    }
  | {
      kind: "verdict";
      verdict: ApprovalVerdict;
      probabilities: ApprovalProbabilities;
    }
  | {
      kind: "irreversibility";
      score: number;
      threshold: number;
      passed: boolean;
    }
  | {
      kind: "outcome";
      verdict: ApprovalVerdict;
    };

/** One completed automatic approval judgment, reconstructed from its durable event. */
export interface ApprovalDecision {
  key: string;
  evaluationId: string;
  workflowId: string;
  agentLabel: string;
  role: ReplayTimelineEntry["role"];
  turnNumber: number;
  timestamp: number;
  toolId: string;
  toolName: string;
  evaluator: string;
  verdict: ApprovalVerdict;
  evaluatorVerdict: ApprovalVerdict;
  reason: string | null;
  confidence: number | null;
  confidenceThreshold: number | null;
  irreversible: number | null;
  irreversibleThreshold: number | null;
  probabilities: ApprovalProbabilities;
  criteriaSet: string | null;
  criteriaVersion: number | null;
  model: string | null;
  requestId: string | null;
  stages: ApprovalDecisionStage[];
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function unitInterval(value: unknown): number | null {
  return typeof value === "number" &&
    Number.isFinite(value) &&
    value >= 0 &&
    value <= 1
    ? value
    : null;
}

function verdictValue(value: unknown): ApprovalVerdict | null {
  return value === "approve" || value === "deny" || value === "escalate"
    ? value
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readProbabilities(details: JsonRecord): ApprovalProbabilities {
  const values = objectValue(details.probabilities);
  return {
    approve: unitInterval(values?.approve),
    deny: unitInterval(values?.deny),
    escalate: unitInterval(values?.escalate)
  };
}

/**
 * Rebuild the path the evaluator took, in the same order as the policy:
 * confidence first, then the reported label, then irreversibility for an
 * approval. A failed confidence gate stops the path immediately — showing the
 * later checks as if they ran would make the audit more certain than the event.
 */
function decisionStages(input: {
  verdict: ApprovalVerdict;
  evaluatorVerdict: ApprovalVerdict;
  confidence: number | null;
  confidenceThreshold: number | null;
  irreversible: number | null;
  irreversibleThreshold: number | null;
  probabilities: ApprovalProbabilities;
}): ApprovalDecisionStage[] {
  const stages: ApprovalDecisionStage[] = [];
  let confidencePassed: boolean | null = null;

  if (input.confidence !== null) {
    confidencePassed =
      input.confidenceThreshold === null
        ? null
        : input.confidence >= input.confidenceThreshold;
    stages.push({
      kind: "confidence",
      score: input.confidence,
      threshold: input.confidenceThreshold,
      passed: confidencePassed
    });
  }

  if (confidencePassed !== false) {
    stages.push({
      kind: "verdict",
      verdict: input.evaluatorVerdict,
      probabilities: input.probabilities
    });

    if (
      input.evaluatorVerdict === "approve" &&
      input.irreversible !== null &&
      input.irreversibleThreshold !== null
    ) {
      stages.push({
        kind: "irreversibility",
        score: input.irreversible,
        threshold: input.irreversibleThreshold,
        /* The backend escalates only when the score is strictly ABOVE the ceiling. */
        passed: input.irreversible <= input.irreversibleThreshold
      });
    }
  }

  stages.push({ kind: "outcome", verdict: input.verdict });
  return stages;
}

/** Completed decisions visible at the replay cursor, in event order. */
export function buildApprovalDecisions(
  timeline: readonly ReplayTimelineEntry[]
): ApprovalDecision[] {
  const decisions: ApprovalDecision[] = [];

  for (const entry of timeline) {
    const frame = entry.frame;
    if (frame.event !== "auto_approval_evaluation_ended") continue;

    const details = frame.data.details;
    const thresholds = objectValue(details.thresholds);
    const verdict = frame.data.verdict;
    const evaluatorVerdict = verdictValue(details.verdict) ?? verdict;
    const confidence = unitInterval(details.confidence);
    const confidenceThreshold = unitInterval(thresholds?.min_confidence);
    const irreversible = unitInterval(details.irreversible);
    const irreversibleThreshold = unitInterval(
      thresholds?.escalate_if_irreversible_above
    );
    const probabilities = readProbabilities(details);

    decisions.push({
      key: `${entry.workflowId}:${frame.data.evaluation_id}`,
      evaluationId: frame.data.evaluation_id,
      workflowId: entry.workflowId,
      agentLabel: entry.label ?? (entry.role === "subagent" ? "Subagent" : "Agent"),
      role: entry.role,
      turnNumber: frame.data.turn_number,
      timestamp: frame.data.timestamp,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      evaluator: frame.data.evaluator,
      verdict,
      evaluatorVerdict,
      reason: frame.data.reason,
      confidence,
      confidenceThreshold,
      irreversible,
      irreversibleThreshold,
      probabilities,
      criteriaSet: stringValue(details.criteria_set),
      criteriaVersion: finiteNumber(details.criteria_version),
      model: stringValue(details.model),
      requestId: stringValue(details.request_id),
      stages: decisionStages({
        verdict,
        evaluatorVerdict,
        confidence,
        confidenceThreshold,
        irreversible,
        irreversibleThreshold,
        probabilities
      })
    });
  }

  return decisions;
}
