// What a session's event stream is projected into: one message per inbound message, holding
// what was sent and, as ordered parts, everything the agent did in answer to it.

import type {
  AutoApprovalVerdict,
  JsonRecord,
  MessageDisposition,
  TextAnnotation,
  TokenUsage
} from "./frames.ts";
import type { AgentSchema, HandlerInput, HandlerName, HandlerOutput, UntypedAgent } from "./schema.ts";

/**
 * - `sending`: submitted, and the server has not admitted it yet.
 * - `accepted`: admitted, and its handler has not started — `disposition` says whether it
 *   opened a turn, joined the open one, or is queued behind it.
 * - `running`: its handler is running.
 * - `done` / `error`: its handler returned or raised, or the submit failed.
 */
export type MessageStatus = "sending" | "accepted" | "running" | "done" | "error";

interface MessageFields {
  /** The `message_id` the harness minted for it, or a local id while it is `sending`. */
  id: string;
  status: MessageStatus;
  /** What it did to the turn when admitted; null while `sending`. */
  disposition: MessageDisposition | null;
  /** The turn it opened, joined or queued for; null while `sending`. */
  turnNumber: number | null;
  /** Everything the agent did in answer to this message, in stream order. */
  parts: MessagePart[];
  /** The handler's error, or why the submit failed. */
  error: string | null;
  acceptedAt: number | null;
  startedAt: number | null;
  endedAt: number | null;
}

/** One inbound message and its answer, narrowed on `handler` to that handler's types. */
export type HarnessMessage<A extends AgentSchema = UntypedAgent> = {
  [H in HandlerName<A>]: MessageFields & {
    handler: H;
    input: HandlerInput<A, H>;
    /** The handler's reply, once it has returned. */
    output: HandlerOutput<A, H> | null;
  };
}[HandlerName<A>];

export type MessagePart =
  | ModelInteractionPart
  | ReplyDeltaPart
  | ThoughtSummaryPart
  | TextAnnotationPart
  | ToolPart
  | SubagentPart;

/** One model call. The parts after it, up to the next model interaction, came out of it. */
export interface ModelInteractionPart {
  type: "model_interaction";
  model: string | null;
  status: "running" | "done";
  usage: TokenUsage | null;
}

/** The model's streamed reply text, from `reply_delta`: not the handler's `output`. */
export interface ReplyDeltaPart {
  type: "reply_delta";
  text: string;
}

export interface ThoughtSummaryPart {
  type: "thought_summary";
  text: string;
}

export interface TextAnnotationPart {
  type: "text_annotation";
  annotation: TextAnnotation;
}

/**
 * - `requested`: the model asked for it; it has not started.
 * - `awaiting_approval`: gated, and waiting on a person.
 * - `evaluating`: gated, and an automatic evaluator is deciding. A person can still answer.
 * - `approved` / `denied`: the gate was settled; a denied call ends here.
 * - `running`, then `awaiting_callback` while a callback tool waits on a client's result.
 * - `done` / `failed`.
 */
export type ToolState =
  | "requested"
  | "awaiting_approval"
  | "evaluating"
  | "approved"
  | "denied"
  | "running"
  | "awaiting_callback"
  | "done"
  | "failed";

export interface ToolPart {
  type: "tool";
  toolId: string;
  toolName: string;
  state: ToolState;
  input: JsonRecord;
  /** The tool's result as the harness rendered it for the model. */
  output: string | null;
  error: string | null;
  /** The latest progress report from a tool still running. */
  progress: string | null;
  approval: { approved: boolean; reason: string | null; remember: boolean } | null;
  /** Every automatic evaluation of this call's gate, in the order they started. */
  evaluations: Evaluation[];
  /** Set for a callback tool: what the client must return, and how the wait ended. */
  callback: { outputSchema: JsonRecord; outcome: "ok" | "error" | "timeout" | null } | null;
}

export interface Evaluation {
  id: string;
  evaluator: string;
  status: "running" | "ended" | "superseded" | "error";
  verdict: AutoApprovalVerdict | null;
  reason: string | null;
  details: JsonRecord;
  error: string | null;
}

/** One message this agent sent a subagent, and whether its reply has come back. */
export interface SubagentPart {
  type: "subagent";
  subagentId: string;
  agentKey: string;
  workflowId: string;
  handler: string;
  subagentTurn: number;
  /** `unavailable`: the subagent's own events could not be read; its reply may still arrive. */
  status: "running" | "ok" | "error" | "unavailable";
  /** The message the subagent ran for this turn, in its own agent view, once it has arrived. */
  messageId: string | null;
}
