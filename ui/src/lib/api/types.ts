import type * as Protocol from "../../../../packages/client/src/protocol";

export type UnixEpochSeconds = number;
export type ResumeOffset = number;
export type StreamOffset = ResumeOffset;

/**
 * One event's own offset in the log of the agent that published it. With the
 * tree-unique `agent_id` this IDENTIFIES the event — it is stable across
 * redeliveries and distinct between two events of the same agent, neither of
 * which is true of `resume_offset` (that one is a root-stream resume cursor and
 * stands still for the whole of a subagent's turn, so every event in that turn
 * reports the same value).
 *
 * `SYNTHESIZED` when the server made the event up rather than reading it off a
 * log, so it has no durable coordinate to report. Absent entirely from mock
 * fixtures and from any server predating the field, which is why it is optional
 * on the envelope and every reader has to cope with it missing.
 */
export type EventOffset = number;

/** An event the server synthesized, which therefore has no `event_offset`. */
export const SYNTHESIZED: EventOffset = -1;
export type WorkflowId = string;
export type TurnId = string;
/**
 * Identifies ONE inbound message for its whole life — returned when it is submitted, and
 * stamped on the envelope of every event its dispatch produces.
 *
 * Not interchangeable with `TurnId`. A turn is the interval the agent is non-idle and is
 * refcounted, so a `mid_turn: "accept"` message joins the turn already running and several
 * messages then publish under one `turn_id`. Anything that pairs a reply with what asked for
 * it, or attributes a streamed delta, keys on this.
 */
export type MessageId = string;
export type ToolId = string;
export type AgentWorkflowType = string;

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonRecord = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Agent registry and sessions
// ---------------------------------------------------------------------------

/**
 * Whether a worker is actually polling an agent's task queue, as of the last look.
 *
 * `ready` is the whole answer, not a hedge: Temporal requires every worker on a task
 * queue to register the same workflow types, so in a supported configuration a poller
 * on the queue serves every agent declared for it. `unknown` is only ever the server
 * failing to ask — never the server asking and being unsure.
 */
export type AgentWorkerStatus = "ready" | "no_worker" | "unknown";

export interface AgentWorker {
  status: AgentWorkerStatus;
  task_queue: string;
  poller_count: number;
  /** Most recent poll across live pollers, epoch seconds. Null when there are none. */
  last_seen: UnixEpochSeconds | null;
  /** Populated only for `unknown` — why the describe call failed. */
  error: string | null;
}

export interface AgentDescriptor {
  key: string;
  workflow_type: AgentWorkflowType;
  task_queue: string;
  label: string;
  description: string;
  /** Absent from older servers, and from fixtures that predate worker readiness. */
  worker?: AgentWorker;
}

export interface AgentRegistryResponse {
  agents: AgentDescriptor[];
}

export interface Session {
  workflow_id: WorkflowId;
  created_at: UnixEpochSeconds;
  label: string;
  agent_workflow_type: AgentWorkflowType;
  is_discovered?: boolean;
  initial_user_message?: string | null;
  execution_status?: string | null;
  closed?: boolean;
}

export type SessionsResponse = Session[];

export interface WorkflowExecutionState {
  workflow_id: WorkflowId;
  execution_status: string;
  closed: boolean;
}

export interface CreateSessionRequest {
  agent_workflow_type: AgentWorkflowType;
}

export type CreateSessionResponse = Session;

// ---------------------------------------------------------------------------
// Accepted inbound messages
// ---------------------------------------------------------------------------

/**
 * What happens to a message that arrives while the agent already has a turn open.
 * Declared per handler by the agent author, so the UI can tell the user whether sending
 * will queue behind the current work, join it, or be refused.
 */
export type MidTurn = "enqueue" | "reject" | "accept";

/**
 * What an admitted message actually DID to the turn — the outcome half of `MidTurn`'s
 * declaration, reported both on the submit response and on the `message_accepted` event.
 *
 * An idle agent gives every mode the same answer (`"opened"`), so what a handler declares
 * only decides the busy case: `"enqueue"` → `"queued"`, `"accept"` → `"joined"`.
 */
export type MessageDisposition = Protocol.MessageDisposition;

/**
 * One `@agent.accepts` handler, as returned by the `agent_interface` discovery query.
 *
 * This is the ONLY thing the UI knows about an agent's inbound surface. Every handler the
 * agent declares appears here, and `parameters` (a JSON Schema of the handler's input model)
 * fully describes the payload — so the composer can render an arbitrary agent without any
 * hardcoded handler name or field name.
 *
 * `model_callable` is the author's hint that a parent agent's model may drive this handler.
 * It is advisory (a parent's own policy decides), and the UI shows it only as a label.
 */
export interface AgentInterfaceFunction {
  name: string;
  description: string;
  parameters: JsonRecord;
  output: JsonRecord;
  mid_turn: MidTurn;
  model_callable: boolean;
}

export interface AgentMessageObject {
  type: string;
  [key: string]: unknown;
}

export type AgentInboundMessage = string | AgentMessageObject;

/**
 * A message addressed to a named handler. There are no per-agent message types in the UI:
 * the handler name comes from `agent_interface` and the payload is built from its schema.
 */
export interface HandlerMessage extends AgentMessageObject {
  type: string;
  payload: JsonRecord;
}

// ---------------------------------------------------------------------------
// Chat, attach, approval, and status endpoints
// ---------------------------------------------------------------------------

export interface ChatRequest {
  session_id: WorkflowId;
  message: AgentInboundMessage;
}

export interface SubmitMessageResponse {
  turn_number: number;
  turn_id: TurnId;
  /** Correlates this message with its own events on the stream — see {@link MessageId}. */
  message_id: MessageId;
  accepted_offset: StreamOffset;
  disposition: MessageDisposition;
}

export interface ToolApprovalRequest {
  session_id: WorkflowId;
  tool_id: ToolId;
  approved: boolean;
  reason?: string | null;
  remember?: boolean;
}

export interface ToolApprovalResponse {
  tool_id: ToolId;
  accepted: true;
}

export interface PendingTurn {
  turn_number: number;
  turn_id: TurnId;
  message_id: MessageId;
  message: string;
}

export interface PendingApproval {
  tool_id: ToolId;
  tool_name: string;
  tool_input: JsonRecord;
  turn_number: number;
  /**
   * Short session-unique alias for `tool_id`, for surfaces that must round-trip the identity
   * through a length-capped field (chat-platform button payloads). Unused by this UI, which
   * carries the full `tool_id`.
   */
  short_id: string;
}

export interface ToolApprovalPolicy {
  dangerously_skip_all_approvals: boolean;
  auto_approve_inherently_safe: boolean;
  auto_approve_tools: string[];
  /**
   * Auto mode: the third way a call can be allow-listed, and the dynamic one — an
   * evaluator judges each call's arguments rather than only its name. Whatever it
   * declines to approve still goes to a human, so this only ever reduces how many
   * approvals a person sees. Not rendered yet; carried so a client can.
   */
  auto_mode_enabled: boolean;
}

export interface SubagentInfo {
  subagent_id: string;
  agent_key: string;
  workflow_id: string;
}

export interface AgentStatusResponse {
  current_turn: number;
  turn_active: boolean;
  pending_turns: PendingTurn[];
  /** Messages running inside the open turn — >1 when `accept` handlers have joined it. */
  turn_participants: number;
  pending_approvals: PendingApproval[];
  subagents: SubagentInfo[];
  approval_policy: ToolApprovalPolicy;
  has_auto_approval_evaluator: boolean;
}

export interface ApiErrorResponse {
  error: string;
  message: string;
}

export interface FastApiValidationErrorResponse {
  detail: unknown;
}

// ---------------------------------------------------------------------------
// SSE stream events
// ---------------------------------------------------------------------------
//
// Every payload is the generated protocol type for its `type`, flattened together with the
// envelope fields `web/app.py` adds to it. A field added to an event in `events.py` reaches
// these types by regenerating `packages/client/src/protocol.ts`. The only hand-written
// parts are what the wire leaves open: the envelope fields below, the few payload fields
// narrowed in `Narrowed`, and `stream_error`, which no agent publishes.

export type AgentEventType = Protocol.AgentEventType;
export type TokenUsage = Protocol.TokenUsage;

export interface AgentEventMetadata extends Omit<Protocol.AgentEvent, "event"> {
  resume_offset: ResumeOffset;
  event_offset?: EventOffset;
  /**
   * This event was already durable when the stream opened, so its delivery is
   * catching this client up rather than showing it something happening now.
   *
   * On the wire only when true, so absent means live — which is also what a
   * server predating the field means, and what the per-turn chat path means by
   * never being a catch-up. Read it as `=== true` and no version check is
   * needed.
   */
  replay?: true;
}

type Payload<TType extends AgentEventType> = Extract<Protocol.AgentStreamItem, { type: TType }>;

/** Payload fields the wire types as an open JSON object, given the shape they carry. */
interface Narrowed {
  text_annotation: { delta: TextAnnotationDelta };
  state_patch: { ops: JsonPatchOp[] };
}

/** The flattened SSE `data` of one event type. */
export type AgentEventData<TType extends AgentEventType> = (TType extends keyof Narrowed
  ? Omit<Payload<TType>, keyof Narrowed[TType]> & Narrowed[TType]
  : Payload<TType>) &
  AgentEventMetadata;

export interface TextAnnotationDelta {
  annotations?: TextAnnotation[];
  [key: string]: unknown;
}

export type TextAnnotation = FileCitationAnnotation | JsonRecord;

export interface FileCitationAnnotation {
  type: "file_citation";
  start_index?: number;
  end_index?: number;
  file_name?: string;
  document_uri?: string;
  custom_metadata?: CitationMetadata;
  [key: string]: unknown;
}

export interface CitationMetadata {
  deep_url?: string;
  page_url?: string;
  anchor?: string;
  heading?: string;
  section_path?: string[];
  section_index?: number;
  section_count?: number;
  title?: string;
  path?: string;
  [key: string]: unknown;
}

/**
 * One RFC 6902 operation off an agent's observable state.
 *
 * The harness derives these — nothing in a workflow author's code builds one —
 * and its own docs promise it emits only these three kinds, never `move`,
 * `copy` or `test`. Typed to that promise rather than to the whole of RFC 6902,
 * so a reader of this file learns what actually arrives; the applier still
 * refuses an op it does not recognize rather than trusting the annotation.
 */
export interface JsonPatchOp {
  op: "add" | "replace" | "remove";
  /** RFC 6901 pointer. `""` is the whole document; a trailing `/-` appends. */
  path: string;
  /** Absent on `remove`, and only on `remove`. */
  value?: JsonValue;
}

// Emitted by POST /api/chat for a client-side timeout, or for this turn's own
// message_handler_error surfaced as the caller's failure. Not an agent event: nothing
// published it on the stream, so it carries no type or turn/message metadata.
export interface ClientSideStreamErrorEvent {
  kind: "timeout" | "agent";
  message: string;
  resume_offset: ResumeOffset;
}

export type AgentSseEventMap = { [TType in AgentEventType]: AgentEventData<TType> } & {
  stream_error: ClientSideStreamErrorEvent;
};

export type AgentStreamEventData =
  AgentSseEventMap[keyof AgentSseEventMap];

export type AgentSseFrame = {
  [TType in keyof AgentSseEventMap]: {
    event: TType;
    data: AgentSseEventMap[TType];
  };
}[keyof AgentSseEventMap];

export type NormalAgentStreamEventData = Exclude<
  AgentStreamEventData,
  ClientSideStreamErrorEvent
>;

export function isClientSideStreamError(
  event: AgentStreamEventData,
): event is ClientSideStreamErrorEvent {
  return "kind" in event && !("type" in event);
}
