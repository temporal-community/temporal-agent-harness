// The builtin auto mode evaluator, backed by TypeSafe's Jev. Workflow-safe: it builds plain JSON
// and dispatches the model call as the `jev_tool_approval` activity by name (see worker.ts).

import { proxyActivities, type ActivityOptions } from "@temporalio/workflow";
import {
  ApprovalVerdict,
  type AutoApprovalContext,
  type AutoApprovalCriteriaSet,
  type AutoApprovalDecision,
  type AutoModeEvaluator,
} from "../../harness/agent-protocol/index.ts";

export const JEV_TOOL_APPROVAL_ACTIVITY = "jev_tool_approval";
export const DEFAULT_JEV_MODEL = "jev-latest";

export interface JevApprovalRequest {
  state: Record<string, unknown>;
  questions: Record<string, Record<string, unknown>>;
  model: string | null;
}

export interface JevApprovalAnswer {
  model: string;
  request_id: string;
  verdict: string;
  verdict_confidence: number;
  verdict_probabilities: Record<string, number>;
  irreversible: number;
  input_tokens: number | null;
  output_tokens: number | null;
}

export type ExtraState =
  | Record<string, unknown>
  | ((ctx: AutoApprovalContext) => Record<string, unknown>);

/** The jev_tool_approval activity's options; `jevEvaluator({activityOptions})` merges over them. */
export const DEFAULT_ACTIVITY_OPTIONS: ActivityOptions = {
  startToCloseTimeout: "30 seconds",
  retry: { maximumAttempts: 3 },
  summary: "jev_approval",
};

export function jevEvaluator({
  model = DEFAULT_JEV_MODEL,
  extraState,
  activityOptions,
}: {
  model?: string | null;
  extraState?: ExtraState;
  activityOptions?: ActivityOptions;
} = {}): AutoModeEvaluator {
  const options: ActivityOptions = { ...DEFAULT_ACTIVITY_OPTIONS, ...activityOptions };
  const approve: AutoModeEvaluator = async (ctx) => {
    const extra = typeof extraState === "function" ? extraState(ctx) : extraState;
    const { jev_tool_approval } = proxyActivities<{
      jev_tool_approval(r: JevApprovalRequest): Promise<JevApprovalAnswer>;
    }>(options);
    const answer = await jev_tool_approval(buildRequest(ctx, { model, extraState: extra }));
    const [minConfidence, irreversibleCeiling] = ctx.thresholds;
    return decide(answer, {
      minConfidence,
      escalateIfIrreversibleAbove: irreversibleCeiling,
      criteriaSetName: ctx.criteria_set_name,
      criteriaVersion: ctx.criteria_version,
    });
  };
  approve.evaluatorLabel = "jev_evaluator";
  return approve;
}

function rules(set: AutoApprovalCriteriaSet, ctx: AutoApprovalContext): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (set.effect) out.what_calls_to_this_tool_do = set.effect;
  if (set.approve_when.length) out.approve_when = [...set.approve_when];
  if (set.deny_when.length) out.deny_when = [...set.deny_when];
  if (set.escalate_when.length) out.escalate_when = [...set.escalate_when];
  if (Object.keys(ctx.criteria.context).length) out.operating_context = { ...ctx.criteria.context };
  return out;
}

export function buildRequest(
  ctx: AutoApprovalContext,
  {
    model = DEFAULT_JEV_MODEL,
    extraState,
  }: { model?: string | null; extraState?: Record<string, unknown> | undefined } = {},
): JevApprovalRequest {
  const state: Record<string, unknown> = {
    situation:
      "An AI agent wants to run a tool. You are the approval gate that decides whether the call may run unattended, must be " +
      "refused, or has to be shown to a person first. The agent chose this tool and these arguments itself.",
    approval_criteria: rules(ctx.criteria_set, ctx),
    tool: {
      name: ctx.tool_name,
      description: ctx.tool_description,
      declared_inherently_safe: ctx.inherently_safe,
    },
    call_arguments: ctx.tool_input,
  };
  if (extraState && Object.keys(extraState).length) state.agent_state = { ...extraState };
  return {
    state,
    model,
    questions: {
      verdict: {
        type: "choice",
        instructions: {
          question:
            "Under the operator's rules in `approval_criteria`, and those rules only, what should happen to the call described " +
            "by `tool` and `call_arguments`?",
          priorities: [
            "Judge the call as it would actually run: `tool.description` says what the tool does, and `call_arguments` are the exact arguments it would run with.",
            "`approval_criteria` is the operator's decision and the only authority here. Do not substitute your own sense of what is risky for what it says — neither to approve something it forbids nor to refuse something it allows.",
            "`approval_criteria.deny_when` and `.escalate_when` are binding. `.approve_when` describes what a routine call looks like; it permits nothing that a deny or escalate rule also matches, so when rules conflict the stricter one wins.",
            "An absent key in `approval_criteria` means the operator said nothing about it. Silence is never permission.",
            "`tool.declared_inherently_safe` is the tool author's own claim that the tool is never unsafe under any input. Treat it as a hint; it never overrides `approval_criteria`.",
            "If the rules do not settle this call, choose `escalate`. A person is available and asking costs almost nothing; guessing does not.",
          ],
        },
        criteria: {
          approve: {
            what: "Run the call now, unattended, with no human review.",
            when: "`approval_criteria` permits calls of this kind, and nothing in `call_arguments` crosses a line the criteria draw.",
            not_for:
              "A call the criteria simply do not address — silence is not permission; that is `escalate`.",
          },
          deny: {
            what: "Refuse the call. It never runs, and the agent is told it was refused so it can try a different approach.",
            when: "`approval_criteria` rule out calls of this kind, or `call_arguments` show this particular call doing something the criteria forbid.",
            not_for:
              "A call that merely looks consequential but that the criteria do not forbid; that is `escalate`.",
          },
          escalate: {
            what: "Hold the call and put it in front of a person, who approves or denies it themselves.",
            when: "`approval_criteria` do not settle this call: they are silent on this kind of call, they are ambiguous, or `call_arguments` are not specific enough to tell which side of the criteria the call falls on.",
            not_for:
              "A call the criteria plainly permit or plainly forbid — deciding those is the point of having them.",
          },
        },
      },
      irreversible: {
        type: "noul",
        instructions:
          "Independently of whether the call should be allowed: if this call runs and turns out to have been a mistake, is its effect one that cannot be taken back?",
        criteria: {
          true: "Running it causes something that cannot be undone from inside this system afterwards — data or a resource destroyed, money moved, a message or request sent to someone outside it.",
          false:
            "It only reads or computes, or whatever it changes can be changed back by a later call.",
        },
      },
    },
  };
}

const pct = (x: number) => `${Math.round(x * 100)}%`;
// Jev answers in the wire vocabulary its questions offer.
const VERDICTS: Record<string, ApprovalVerdict> = {
  approve: "approved",
  deny: "denied",
  escalate: "user-approval",
};

/** Turn Jev's answer into a decision under the operator's thresholds. Escalates when unsure. */
export function decide(
  answer: JevApprovalAnswer,
  opts: {
    minConfidence: number;
    escalateIfIrreversibleAbove: number | null;
    criteriaSetName?: string;
    criteriaVersion?: number;
  },
): AutoApprovalDecision {
  const verdict = VERDICTS[answer.verdict];
  const stats = `jev/${answer.model}: verdict=${answer.verdict} (confidence ${pct(answer.verdict_confidence)}), irreversible ${pct(answer.irreversible)}`;
  const details = {
    model: answer.model,
    request_id: answer.request_id,
    verdict: answer.verdict,
    confidence: answer.verdict_confidence,
    probabilities: { ...answer.verdict_probabilities },
    irreversible: answer.irreversible,
    usage: { input_tokens: answer.input_tokens, output_tokens: answer.output_tokens },
    thresholds: {
      min_confidence: opts.minConfidence,
      escalate_if_irreversible_above: opts.escalateIfIrreversibleAbove,
    },
    criteria_set: opts.criteriaSetName ?? "",
    criteria_version: opts.criteriaVersion ?? 0,
  };
  const escalate = (why: string): AutoApprovalDecision => ({
    verdict: ApprovalVerdict.USER_APPROVAL,
    reason: `${stats} — ${why}`,
    details,
  });
  if (answer.verdict_confidence < opts.minConfidence) {
    return escalate(
      `below the ${pct(opts.minConfidence)} confidence needed to decide automatically; escalated to a human.`,
    );
  }
  if (verdict === undefined) return escalate("unrecognized verdict; escalated to a human.");
  if (verdict === ApprovalVerdict.USER_APPROVAL)
    return escalate("the approval criteria do not settle this call.");
  if (verdict === ApprovalVerdict.DENIED) {
    return {
      verdict: ApprovalVerdict.DENIED,
      reason: `${stats} — the approval criteria forbid this call.`,
      details,
    };
  }
  if (
    opts.escalateIfIrreversibleAbove !== null &&
    answer.irreversible > opts.escalateIfIrreversibleAbove
  ) {
    return escalate(
      `approved on the criteria, but the effect looks irreversible (over the ${pct(opts.escalateIfIrreversibleAbove)} ceiling); escalated to a human.`,
    );
  }
  return { verdict: ApprovalVerdict.APPROVED, reason: `${stats} — auto-approved.`, details };
}
