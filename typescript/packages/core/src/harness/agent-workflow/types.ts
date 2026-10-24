import type { z } from "zod";
import type { RawFormat } from "../../ai-sdks/integration-helpers/raw-events.ts";
import type { AgentMessage, AgentStreamItem, AutoApprovalCriteria, AutoModeEvaluator, MessageContext, MidTurn, Schema, ToolApprovalPolicy } from "../agent-protocol/index.ts";
import type { StateDecl } from "../state/index.ts";

/** One `@agent.accepts`-style handler declaration: the static half, discoverable without a run. */
export interface HandlerDecl<I extends z.ZodType = z.ZodType, O extends z.ZodType = z.ZodType> {
  readonly description: string;
  readonly input: I;
  readonly output: O;
  readonly midTurn: MidTurn;
  readonly modelCallable: boolean;
  /** The input's parameter name (Python's handler parameter); a subagent send tool's argument. */
  readonly inputName: string;
}

export type HandlerImpl<D extends HandlerDecl> = (
  message: z.output<D["input"]>,
  ctx: MessageContext,
) => Promise<z.input<D["output"]>>;

/**
 * What an SDK integration (AI SDK, OpenAI Agents, Gemini, a hand-written loop) needs from the
 * harness inside the workflow: wrap each tool's `execute` with `runTool`, publish reply events
 * with `emit`, and run each model call through `modelCall` so its raw stream lands on the turn.
 */
export interface TurnHost {
  readonly tools: readonly AnyHarnessTool[];
  /** Run one call by tool name: approval gate, callbacks and tool_* events included. */
  runTool(name: string, input: unknown, options?: { toolId?: string }): Promise<unknown>;
  emit(event: AgentStreamItem): void;
  /**
   * `call` gets the stream topic its raw model events must be published to. A `structuredOutput`
   * call's text is a JSON document, not prose, so it streams no reply_delta: the handler's result
   * is the reply.
   */
  modelCall<T>(
    format: RawFormat,
    model: string | null,
    call: (streamingTopic: string) => Promise<T>,
    options?: { structuredOutput?: boolean },
  ): Promise<T>;
}

export interface Admission {
  message: AgentMessage;
  turn_id: string;
  turn_number: number;
  message_id: string;
  /** Stream offset of the message's message_accepted; absent in runs carried before it was kept. */
  accepted_offset?: number;
}

export interface ApprovalEntry {
  tool_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  turn_number: number;
  turn_id: string;
  message_id: string | null;
  short_id: string;
  inherently_safe: boolean;
  status: "pending" | "approved" | "denied";
  reason: string | null;
  remember: boolean;
}

export interface CallbackEntry {
  tool_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  output: Schema;
  turn_number: number;
  turn_id: string;
  message_id: string | null;
  status: "pending" | "resolved";
  outcome: "ok" | "error" | "timeout";
  result: unknown;
  error: string | null;
}

export interface SubagentInstance {
  handle: string;
  workflow_id: string;
  agent_key: string;
  last_consumed_offset: number;
  next_ticket: number;
  serving: number;
}

export interface RunnerOptions {
  handlers: Readonly<Record<string, HandlerDecl>>;
  approvalPolicyDefault: ToolApprovalPolicy;
  autoApprovalCriteriaDefault?: AutoApprovalCriteria | undefined;
  autoModeEvaluator?: AutoModeEvaluator | undefined;
  states?: Readonly<Record<string, AnyStateDecl>> | undefined;
  /** The agent's validated init data, handed to each state's `initial`. */
  data?: unknown;
  /** The init data as the workflow received it, passed unchanged to the run it continues as. */
}

export type AnyStateDecl = StateDecl<z.ZodType<Record<string, unknown>>>;

// The tool declaration shapes (Python declares its tools in agent_workflow.py).

export type ObjectSchema = Schema<Record<string, unknown>, Record<string, unknown>>;

/**
 * A harness tool: the AI SDK tool shape (`description`, `inputSchema`, optional `outputSchema`,
 * `execute`) plus where it runs and how it is gated. A tool with no `execute` is a callback tool,
 * fulfilled by a client against its required `outputSchema`.
 */
export interface HarnessTool<I = Record<string, unknown>, O = unknown> {
  readonly kind: "inline" | "activity" | "callback";
  readonly name: string;
  readonly description: string;
  readonly inputSchema: ObjectSchema;
  readonly outputSchema: Schema | undefined;
  readonly inherentlySafe: boolean;
  readonly autoApprovalCriteria: string | null;
  readonly injected: readonly string[];
  /** The model-facing JSON schema of the arguments (injected parameters are not in it). */
  readonly parameters: Record<string, unknown>;
  /** Run the call. Only valid inside `runner.runTool(...)`. */
  invoke(input: I): Promise<O>;
}

export type AnyHarnessTool = HarnessTool<never, unknown>;
