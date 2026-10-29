// The script the scripted integration plays in place of a model: one message's steps, as
// python/tests/conformance/script.py declares them. The driver that plays scenarios and checks the
// integration against its scripts is Python's; this module only parses what it sends.

import { z } from "zod";

export const HANDLERS = ["run_reject", "run_enqueue", "run_accept"] as const;
export type Handler = (typeof HANDLERS)[number];

export const TOOL_NAMES = ["add", "echo", "fail", "gated", "ask_client"] as const;

const count = z.number().int().nullable().default(null);
const TokenUsage = z.object({
  input_tokens: count,
  output_tokens: count,
  thought_tokens: count,
  cached_tokens: count,
  tool_use_tokens: count,
  total_tokens: count,
});

/** One model interaction streaming `reply`. */
const ModelStep = z.strictObject({
  step: z.literal("model"),
  model: z.string().default("scripted"),
  reply: z.array(z.string()).default([]),
  usage: TokenUsage.nullable().default(null),
});

/** One tool call the model requests. */
const ToolStep = z.strictObject({
  step: z.literal("tool"),
  id: z.string(),
  name: z.enum(TOOL_NAMES),
  args: z.record(z.string(), z.json()).default({}),
});

const StateSetStep = z.strictObject({
  step: z.literal("state_set"),
  key: z.string(),
  value: z.string(),
});

const StateDeleteStep = z.strictObject({ step: z.literal("state_delete"), key: z.string() });

const SubagentStartStep = z.strictObject({ step: z.literal("subagent_start"), name: z.string() });

const SubagentSendStep = z.strictObject({
  step: z.literal("subagent_send"),
  name: z.string(),
  handler: z.enum(HANDLERS).default("run_reject"),
  get script(): typeof Script {
    return Script;
  },
});

const SubagentStopStep = z.strictObject({ step: z.literal("subagent_stop"), name: z.string() });

/** The handler raises `message`; later steps never run. */
const RaiseStep = z.strictObject({ step: z.literal("raise"), message: z.string() });

const Step = z.discriminatedUnion("step", [
  ModelStep,
  ToolStep,
  StateSetStep,
  StateDeleteStep,
  SubagentStartStep,
  SubagentSendStep,
  SubagentStopStep,
  RaiseStep,
]);

/** The steps one message plays, in order. */
export const Script = z.strictObject({ steps: z.array(Step) });
export type Script = z.output<typeof Script>;

/** The text every model step of the script streamed. */
export const ScriptReply = z.object({ text: z.string() });
