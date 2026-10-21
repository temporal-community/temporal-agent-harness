// The agent's callable interface: the signal/update/query names an agent workflow and its clients
// share, the validated input models (zod, in place of pydantic), and the wire types (Python's
// agent_protocol/agent_interface.py). Every wire shape is snake_case JSON, identical to the Python
// harness, so the packaged UI, `@temporalio/agent-harness-client` and the Go connectors work against
// either backend. Workflow-sandbox safe (zod only).

import { z } from "zod";

export const SEND_AGENT_MESSAGE_UPDATE = "send_agent_message";
export const TOOL_APPROVAL_UPDATE = "tool_approval";
export const PROVIDE_CALLBACK_RESULT_UPDATE = "provide_callback_result";
export const AGENT_STATUS_QUERY = "agent_status";
export const AGENT_INTERFACE_QUERY = "agent_interface";
export const CLOSE_SIGNAL = "close";

export const AGENT_ID_LENGTH = 6;
export const APPROVAL_SHORT_ID_LENGTH = 6;

export const MidTurn = { ENQUEUE: "enqueue", REJECT: "reject", ACCEPT: "accept" } as const;
export type MidTurn = (typeof MidTurn)[keyof typeof MidTurn];

export const MessageDisposition = { OPENED: "opened", JOINED: "joined", QUEUED: "queued" } as const;
export type MessageDisposition = (typeof MessageDisposition)[keyof typeof MessageDisposition];

/** What an auto-mode evaluator decides, named after AI SDK 7's tool-approval outcomes. */
export const ApprovalVerdict = {
  APPROVED: "approved",
  DENIED: "denied",
  USER_APPROVAL: "user-approval",
  NOT_APPLICABLE: "not-applicable",
} as const;
export type ApprovalVerdict = (typeof ApprovalVerdict)[keyof typeof ApprovalVerdict];

/** The verdict as it goes on the wire (auto_approval_evaluation_* events), as in Python. */
export const AutoApprovalVerdict = {
  APPROVE: "approve",
  DENY: "deny",
  ESCALATE: "escalate",
} as const;
export type AutoApprovalVerdict = (typeof AutoApprovalVerdict)[keyof typeof AutoApprovalVerdict];

/** Both of the evaluator's "not mine to decide" outcomes leave the call to a human. */
export const WIRE_VERDICT: Readonly<Record<ApprovalVerdict, AutoApprovalVerdict>> = {
  approved: "approve",
  denied: "deny",
  "user-approval": "escalate",
  "not-applicable": "escalate",
};

const AGENT_ID_PATTERN = new RegExp(
  `^[0-9a-f]{${AGENT_ID_LENGTH}}(-[0-9a-f]{${AGENT_ID_LENGTH}})*$`,
);

// The values pydantic's lax mode coerces, so a config Python accepts parses here too.
const TRUE_STRINGS = new Set(["true", "1", "yes", "on", "t", "y"]);
const FALSE_STRINGS = new Set(["false", "0", "no", "off", "f", "n"]);
const laxBool = z
  .union([
    z.boolean(),
    z.literal([0, 1]),
    z
      .string()
      .refine((s) => TRUE_STRINGS.has(s.toLowerCase()) || FALSE_STRINGS.has(s.toLowerCase()), {
        message: "Input should be a valid boolean, unable to interpret input",
      }),
  ])
  .transform((v) =>
    typeof v === "string" ? TRUE_STRINGS.has(v.toLowerCase()) : typeof v === "number" ? v === 1 : v,
  );
// Python float() syntax for a finite decimal, digits optionally grouped by single underscores.
const DIGITS = String.raw`\d(?:_?\d)*`;
const NUMERIC_STRING = new RegExp(
  String.raw`^\s*[+-]?(?:${DIGITS}(?:\.(?:${DIGITS})?)?|\.${DIGITS})(?:[eE][+-]?${DIGITS})?\s*$`,
);
const unit = z
  .union([
    z.number(),
    z.boolean(),
    z
      .string()
      .regex(NUMERIC_STRING, "Input should be a valid number, unable to parse string as a number"),
  ])
  .transform((v) => Number(typeof v === "string" ? v.replaceAll("_", "") : v))
  .pipe(z.number().min(0).max(1));

// ---------------------------------------------------------------------------------------------
// Tool-approval policy. A plain, serializable value; derive the next one with the functions on
// `ToolApprovalPolicy`, which re-validate (the cross-field invariant included).
// ---------------------------------------------------------------------------------------------

export const ToolApprovalPolicySchema = z
  .strictObject({
    dangerously_skip_all_approvals: laxBool.default(false),
    auto_approve_inherently_safe: laxBool.default(false),
    auto_approve_tools: z
      .array(z.string())
      .default([])
      .transform((tools) => [...new Set(tools)].sort()),
    auto_mode_enabled: laxBool.default(false),
  })
  .refine((p) => !(p.dangerously_skip_all_approvals && p.auto_mode_enabled), {
    message:
      "dangerously_skip_all_approvals=True approves every call outright, so no call is ever gated and " +
      "auto mode could never be consulted. Set one or the other: skip approvals entirely, or enable " +
      "auto mode over a policy that actually gates.",
  });
export type ToolApprovalPolicy = z.output<typeof ToolApprovalPolicySchema>;

export const ToolApprovalPolicy = {
  parse: (value: unknown): ToolApprovalPolicy => ToolApprovalPolicySchema.parse(value),
  alwaysRequireHumanApproval: (): ToolApprovalPolicy => ToolApprovalPolicySchema.parse({}),
  allowInherentlySafe: (): ToolApprovalPolicy =>
    ToolApprovalPolicySchema.parse({ auto_approve_inherently_safe: true }),
  allowTools: (tools: Iterable<string>, { alsoInherentlySafe = false } = {}): ToolApprovalPolicy =>
    ToolApprovalPolicySchema.parse({
      auto_approve_tools: [...tools],
      auto_approve_inherently_safe: alsoInherentlySafe,
    }),
  autoMode: ({
    preApprovedTools = [] as Iterable<string>,
    preApproveInherentlySafe = false,
  } = {}): ToolApprovalPolicy =>
    ToolApprovalPolicySchema.parse({
      auto_approve_tools: [...preApprovedTools],
      auto_approve_inherently_safe: preApproveInherentlySafe,
      auto_mode_enabled: true,
    }),
  dangerouslySkipAll: (): ToolApprovalPolicy =>
    ToolApprovalPolicySchema.parse({ dangerously_skip_all_approvals: true }),
  withChanges: (
    policy: ToolApprovalPolicy,
    changes: Partial<ToolApprovalPolicy>,
  ): ToolApprovalPolicy => ToolApprovalPolicySchema.parse({ ...policy, ...changes }),
  withToolAllowed: (policy: ToolApprovalPolicy, toolName: string): ToolApprovalPolicy =>
    ToolApprovalPolicySchema.parse({
      ...policy,
      auto_approve_tools: [...policy.auto_approve_tools, toolName],
    }),
  withAutoMode: (policy: ToolApprovalPolicy, enabled: boolean): ToolApprovalPolicy =>
    ToolApprovalPolicySchema.parse({ ...policy, auto_mode_enabled: enabled }),
  /** Layers 0-2: dispatch with no gate and no approval events at all. */
  autoApproves(
    policy: ToolApprovalPolicy,
    toolName: string,
    { inherentlySafe }: { inherentlySafe: boolean },
  ): boolean {
    if (policy.dangerously_skip_all_approvals) return true;
    if (inherentlySafe && policy.auto_approve_inherently_safe) return true;
    return policy.auto_approve_tools.includes(toolName);
  },
  /** Layer 3: whether a call the static layers did not approve goes to the auto mode evaluator. */
  consultsAutoMode(
    policy: ToolApprovalPolicy,
    toolName: string,
    opts: { inherentlySafe: boolean },
  ): boolean {
    return !ToolApprovalPolicy.autoApproves(policy, toolName, opts) && policy.auto_mode_enabled;
  },
};

// ---------------------------------------------------------------------------------------------
// Auto-approval criteria: the named rule sets an auto mode evaluator judges gated calls against.
// ---------------------------------------------------------------------------------------------

export const AutoApprovalCriteriaSetSchema = z.object({
  effect: z.string().nullable().default(null),
  approve_when: z.array(z.string()).default([]),
  deny_when: z.array(z.string()).default([]),
  escalate_when: z.array(z.string()).default([]),
  min_confidence: unit.nullable().default(null),
  escalate_if_irreversible_above: unit.nullable().default(null),
});
export type AutoApprovalCriteriaSet = z.output<typeof AutoApprovalCriteriaSetSchema>;

// z.record assigns into a plain object, where a "__proto__" key sets the prototype and vanishes.
const ownRecord = <V extends z.ZodType>(value: V) =>
  z
    .preprocess(
      (v, ctx) => {
        if (typeof v === "object" && v !== null && !Array.isArray(v))
          return new Map(Object.entries(v));
        ctx.addIssue({ code: "invalid_type", expected: "record", input: v });
        return z.NEVER;
      },
      z.map(z.string(), value),
    )
    .transform((m) =>
      Object.assign(Object.create(null) as Record<string, z.output<V>>, Object.fromEntries(m)),
    )
    .prefault({});

export const AutoApprovalCriteriaSchema = z.object({
  sets: ownRecord(AutoApprovalCriteriaSetSchema),
  tools: ownRecord(z.string()),
  default: z.string().nullable().default(null),
  context: ownRecord(z.unknown()),
  min_confidence: unit.default(0.8),
  escalate_if_irreversible_above: unit.nullable().default(0.5),
});
export type AutoApprovalCriteria = z.output<typeof AutoApprovalCriteriaSchema>;

export type Thresholds = readonly [
  minConfidence: number,
  escalateIfIrreversibleAbove: number | null,
];

const own = <V>(record: Record<string, V>, key: string): V | undefined =>
  Object.hasOwn(record, key) ? record[key] : undefined;

export const AutoApprovalCriteria = {
  parse: (value: unknown): AutoApprovalCriteria => AutoApprovalCriteriaSchema.parse(value),
  empty: (): AutoApprovalCriteria => AutoApprovalCriteriaSchema.parse({}),
  criteriaSet: (value: z.input<typeof AutoApprovalCriteriaSetSchema>): AutoApprovalCriteriaSet =>
    AutoApprovalCriteriaSetSchema.parse(value),
  isEmptySet: (s: AutoApprovalCriteriaSet): boolean =>
    !(s.effect || s.approve_when.length || s.deny_when.length || s.escalate_when.length),
  /** Precedence: runtime/config assignment, then the tool's declared set, then the catch-all. */
  setNameFor: (
    c: AutoApprovalCriteria,
    toolName: string,
    declared?: string | null,
  ): string | null => own(c.tools, toolName) || declared || c.default,
  /** The set judging `toolName`, or null when there is nothing to judge against (escalate). */
  forTool(
    c: AutoApprovalCriteria,
    toolName: string,
    declared?: string | null,
  ): AutoApprovalCriteriaSet | null {
    const name = AutoApprovalCriteria.setNameFor(c, toolName, declared);
    const found = name === null ? undefined : own(c.sets, name);
    return found === undefined || AutoApprovalCriteria.isEmptySet(found) ? null : found;
  },
  thresholdsForSet(c: AutoApprovalCriteria, s: AutoApprovalCriteriaSet | null): Thresholds {
    if (s === null) return [c.min_confidence, c.escalate_if_irreversible_above];
    return [
      s.min_confidence ?? c.min_confidence,
      s.escalate_if_irreversible_above ?? c.escalate_if_irreversible_above,
    ];
  },
  withSet: (
    c: AutoApprovalCriteria,
    name: string,
    s: AutoApprovalCriteriaSet,
  ): AutoApprovalCriteria => ({
    ...c,
    sets: Object.assign(Object.create(null), c.sets, { [name]: s }),
  }),
  withToolAssigned: (
    c: AutoApprovalCriteria,
    toolName: string,
    setName: string,
  ): AutoApprovalCriteria => ({
    ...c,
    tools: Object.assign(Object.create(null), c.tools, { [toolName]: setName }),
  }),
};

export interface AutoApprovalDecision {
  verdict: ApprovalVerdict;
  reason?: string | null;
  details?: Record<string, unknown>;
}

export interface AutoApprovalContext {
  tool_name: string;
  tool_input: Record<string, unknown>;
  inherently_safe: boolean;
  tool_description: string | null;
  criteria: AutoApprovalCriteria;
  criteria_version: number;
  criteria_set: AutoApprovalCriteriaSet;
  criteria_set_name: string;
  thresholds: Thresholds;
}

/** Async on purpose: the gate races it against a human and cancels the loser. */
export type AutoModeEvaluator = ((ctx: AutoApprovalContext) => Promise<AutoApprovalDecision>) & {
  /** Published as `evaluator` on the evaluation events; defaults to the function name. */
  evaluatorLabel?: string;
};

// ---------------------------------------------------------------------------------------------
// Standardized agent input and messages
// ---------------------------------------------------------------------------------------------

export const AgentConfigSchema = z.object({
  approval_policy: ToolApprovalPolicySchema.nullish().transform((v) => v ?? null),
  auto_approval_criteria: AutoApprovalCriteriaSchema.nullish().transform((v) => v ?? null),
  agent_id: z
    .string()
    .regex(AGENT_ID_PATTERN)
    .nullish()
    .transform((v) => v ?? null),
});
export type AgentConfig = z.output<typeof AgentConfigSchema>;
export type AgentConfigInput = z.input<typeof AgentConfigSchema>;

// Not z.record, which rebuilds the object and drops a "__proto__" key.
export const messagePayload = z
  .custom<Record<string, unknown>>(
    (v) => typeof v === "object" && v !== null && !Array.isArray(v),
    "Expected an object",
  )
  .default({});

export const AgentMessageSchema = z.object({
  type: z.string(),
  payload: messagePayload,
});
export type AgentMessage = z.output<typeof AgentMessageSchema>;

// Schemas as Python's pydantic models publish them (field titles included), for wire parity.
export const TextMessage = z.object({ text: z.string().meta({ title: "Text" }) }).meta({
  title: "TextMessage",
  description: "Free-form natural-language text from the user (the input to a plain chat handler).",
});
export const TextReply = z.object({ text: z.string().meta({ title: "Text" }) }).meta({
  title: "TextReply",
  description: "A free-form natural-language reply (the output of a plain chat handler).",
});

export interface MessageContext {
  joined_turn: boolean;
  turn_id: string;
  turn_number: number;
  message_id: string;
}

export interface AgentMessageReply {
  turn_number: number;
  turn_id: string;
  message_id: string;
  disposition: MessageDisposition;
  accepted_offset: number;
}

export const ToolApprovalDecisionSchema = z.object({
  tool_id: z.string(),
  approved: z.boolean(),
  reason: z
    .string()
    .nullish()
    .transform((v) => v ?? null),
  remember: z.boolean().default(false),
});
export type ToolApprovalDecision = z.output<typeof ToolApprovalDecisionSchema>;

export interface ToolApprovalResult {
  tool_id: string;
  accepted: boolean;
}

export const CallbackResultSchema = z.object({
  tool_id: z.string(),
  result: z.unknown().default(null),
  error: z
    .string()
    .nullish()
    .transform((v) => v ?? null),
});
export type CallbackResult = z.output<typeof CallbackResultSchema>;

export interface CallbackResultAck {
  tool_id: string;
  accepted: boolean;
}

/** `turn_id` guards against cancelling a turn that opened after the caller looked. */
export interface PendingApproval {
  tool_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  turn_number: number;
  short_id: string;
}

export interface PendingCallback {
  tool_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  turn_number: number;
}

export interface PendingTurn {
  turn_number: number;
  turn_id: string;
  message_id: string;
  message: string;
}

export interface SubagentInfo {
  subagent_id: string;
  agent_key: string;
  workflow_id: string;
}

export interface AgentStatus {
  agent_id: string;
  current_turn: number;
  turn_active: boolean;
  turn_participants: number;
  pending_turns: PendingTurn[];
  pending_approvals: PendingApproval[];
  pending_callbacks: PendingCallback[];
  subagents: SubagentInfo[];
  approval_policy: ToolApprovalPolicy;
  has_auto_approval_evaluator: boolean;
}

/** One turn of the current run (a continued run keeps its last 20), as the `agent_state` query indexes it. */
export interface AcceptedFunction {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  output: Record<string, unknown>;
  mid_turn: MidTurn;
  model_callable: boolean;
}
