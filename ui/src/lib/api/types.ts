export type UnixEpochSeconds = number;
export type ResumeOffset = number;
export type StreamOffset = ResumeOffset;
export type WorkflowId = string;
export type TurnId = string;
export type ToolId = string;
export type AgentWorkflowType = string;

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonRecord = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Agent registry and sessions
// ---------------------------------------------------------------------------

export interface AgentDescriptor {
  key: string;
  workflow_type: AgentWorkflowType;
  task_queue: string;
  label: string;
  description: string;
}

export interface AgentRegistryResponse {
  agents: AgentDescriptor[];
}

export interface Session {
  workflow_id: WorkflowId;
  created_at: UnixEpochSeconds;
  label: string;
  agent_workflow_type: AgentWorkflowType;
  is_message_queuing_enabled: boolean;
  initial_user_message?: string | null;
}

export type SessionsResponse = Session[];

export interface CreateSessionRequest {
  agent_workflow_type: AgentWorkflowType;
  is_message_queuing_enabled?: boolean;
}

export type CreateSessionResponse = Session;

// ---------------------------------------------------------------------------
// Accepted inbound messages
// ---------------------------------------------------------------------------

export interface MessageTypeSchema {
  name: string;
  json_schema: JsonRecord;
}

export interface AcceptedMessageTypesResponse {
  accepts_text: boolean;
  models: MessageTypeSchema[];
}

export interface AgentInterfaceFunction {
  name: string;
  description: string;
  parameters: JsonRecord;
  output: JsonRecord;
}

export interface AgentMessageObject {
  type: string;
  [key: string]: unknown;
}

export type AgentInboundMessage = string | AgentMessageObject;

export type QaSlashCommandPayload =
  | { name: "scope"; arg?: "all" | "docs" | "forum" }
  | { name: "set-model"; arg?: "gemini-3.5-flash" | "gemini-3.1-flash-lite" }
  | { name: "set-docs-store"; arg?: "temporal-docs-v2" }
  | { name: "set-forum-store"; arg?: "temporal-forum" }
  | {
      name: "approval-policy";
      arg?: "always-require" | "allow-safe" | "dangerously-skip-permissions";
    };

export interface QaSlashCommandMessage extends AgentMessageObject {
  type: "slash_command";
  payload: QaSlashCommandPayload;
}

export interface MontyRunScriptMessage extends AgentMessageObject {
  type: "run_script";
  payload: {
    script: string;
  };
}

export type KnownAgentMessage = QaSlashCommandMessage | MontyRunScriptMessage;

// ---------------------------------------------------------------------------
// Chat, attach, approval, and status endpoints
// ---------------------------------------------------------------------------

export interface ChatRequest {
  session_id: WorkflowId;
  message: AgentInboundMessage;
  expected_turn: number;
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
  message: string;
}

export interface PendingApproval {
  tool_id: ToolId;
  tool_name: string;
  tool_input: JsonRecord;
  turn_number: number;
}

export interface ToolApprovalPolicy {
  dangerously_skip_all_approvals: boolean;
  auto_approve_inherently_safe: boolean;
  auto_approve_tools: string[];
}

export interface SubagentInfo {
  subagent_id: string;
  agent_key: string;
  workflow_id: string;
  next_expected_turn: number;
}

export interface AgentStatusResponse {
  current_turn: number;
  turn_active: boolean;
  pending_turns: PendingTurn[];
  is_message_queuing_enabled: boolean;
  pending_approvals: PendingApproval[];
  subagents: SubagentInfo[];
  approval_policy: ToolApprovalPolicy;
  has_custom_approval_fallback: boolean;
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

export type AgentEventType =
  | "message_queued"
  | "turn_started"
  | "turn_end"
  | "model_interaction_started"
  | "model_interaction_ended"
  | "tool_requested"
  | "tool_approval_requested"
  | "tool_approval_resolved"
  | "tool_start"
  | "tool_progress_delta"
  | "tool_end"
  | "tool_error"
  | "subagent_started"
  | "subagent_stopped"
  | "subagent_message_sent"
  | "subagent_reply_received"
  | "subagent_stream_unavailable"
  | "reply_delta"
  | "thought_summary"
  | "text_annotation"
  | "reply"
  | "error";

export interface AgentEventMetadata {
  agent_id: string;
  turn_id: TurnId;
  turn_number: number;
  timestamp: UnixEpochSeconds;
  resume_offset: ResumeOffset;
}

export interface AgentEventDataBase<TType extends AgentEventType>
  extends AgentEventMetadata {
  type: TType;
}

export interface MessageQueuedEvent
  extends AgentEventDataBase<"message_queued"> {
  user_message: string;
}

export interface TurnStartedEvent extends AgentEventDataBase<"turn_started"> {
  user_message: string;
}

export interface TurnEndEvent extends AgentEventDataBase<"turn_end"> {}

export interface TokenUsage {
  input_tokens?: number | null;
  output_tokens?: number | null;
  thought_tokens?: number | null;
  cached_tokens?: number | null;
  tool_use_tokens?: number | null;
}

export interface ModelInteractionStartedEvent
  extends AgentEventDataBase<"model_interaction_started"> {
  model: string | null;
}

export interface ModelInteractionEndedEvent
  extends AgentEventDataBase<"model_interaction_ended"> {
  model: string | null;
  usage: TokenUsage | null;
}

export interface ToolEventDataBase<TType extends AgentEventType>
  extends AgentEventDataBase<TType> {
  tool_id: ToolId;
  tool_name: string;
}

export interface ToolRequestedEvent
  extends ToolEventDataBase<"tool_requested"> {
  tool_input: JsonRecord;
}

export interface ToolApprovalRequestedEvent
  extends ToolEventDataBase<"tool_approval_requested"> {
  tool_input: JsonRecord;
}

export interface ToolApprovalResolvedEvent
  extends ToolEventDataBase<"tool_approval_resolved"> {
  approved: boolean;
  reason: string | null;
  remember: boolean;
}

export interface ToolStartEvent extends ToolEventDataBase<"tool_start"> {
  tool_input: JsonRecord;
}

export interface ToolProgressDeltaEvent
  extends ToolEventDataBase<"tool_progress_delta"> {
  progress_delta: string;
}

export interface ToolEndEvent extends ToolEventDataBase<"tool_end"> {
  tool_output: string;
}

export interface ToolErrorEvent extends ToolEventDataBase<"tool_error"> {
  message: string;
}

export interface SubagentStartedEvent
  extends AgentEventDataBase<"subagent_started"> {
  subagent_id: string;
  agent_key: string;
  workflow_id: string;
}

export interface SubagentStoppedEvent
  extends AgentEventDataBase<"subagent_stopped"> {
  subagent_id: string;
  agent_key: string;
  workflow_id: string;
}

export interface SubagentMessageSentEvent
  extends AgentEventDataBase<"subagent_message_sent"> {
  subagent_id: string;
  agent_key: string;
  workflow_id: string;
  function: string;
  subagent_turn: number;
  from_offset: number;
}

export interface SubagentReplyReceivedEvent
  extends AgentEventDataBase<"subagent_reply_received"> {
  subagent_id: string;
  agent_key: string;
  workflow_id: string;
  function: string;
  subagent_turn: number;
  outcome: "ok" | "error";
}

export interface SubagentStreamUnavailableEvent
  extends AgentEventDataBase<"subagent_stream_unavailable"> {
  subagent_id: string;
  workflow_id: string;
  reason: string;
}

export interface ReplyDeltaEvent extends AgentEventDataBase<"reply_delta"> {
  text: string;
}

export interface ThoughtSummaryEvent
  extends AgentEventDataBase<"thought_summary"> {
  delta: JsonRecord;
}

export interface TextAnnotationEvent
  extends AgentEventDataBase<"text_annotation"> {
  delta: TextAnnotationDelta;
}

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

export interface ReplyEvent extends AgentEventDataBase<"reply"> {
  output?: JsonRecord | null;
  text?: string | null;
}

export interface AgentErrorEvent extends AgentEventDataBase<"error"> {
  message: string;
}

// Emitted by POST /api/chat for client-side timeout or conversion of this
// turn's AgentError. Unlike normal agent events, these may not include type or
// turn metadata.
export interface ClientSideStreamErrorEvent {
  kind: "timeout" | "agent";
  message: string;
  resume_offset: ResumeOffset;
}

export interface AgentSseEventMap {
  message_queued: MessageQueuedEvent;
  turn_started: TurnStartedEvent;
  turn_end: TurnEndEvent;
  model_interaction_started: ModelInteractionStartedEvent;
  model_interaction_ended: ModelInteractionEndedEvent;
  tool_requested: ToolRequestedEvent;
  tool_approval_requested: ToolApprovalRequestedEvent;
  tool_approval_resolved: ToolApprovalResolvedEvent;
  tool_start: ToolStartEvent;
  tool_progress_delta: ToolProgressDeltaEvent;
  tool_end: ToolEndEvent;
  tool_error: ToolErrorEvent;
  subagent_started: SubagentStartedEvent;
  subagent_stopped: SubagentStoppedEvent;
  subagent_message_sent: SubagentMessageSentEvent;
  subagent_reply_received: SubagentReplyReceivedEvent;
  subagent_stream_unavailable: SubagentStreamUnavailableEvent;
  reply_delta: ReplyDeltaEvent;
  thought_summary: ThoughtSummaryEvent;
  text_annotation: TextAnnotationEvent;
  reply: ReplyEvent;
  error: AgentErrorEvent | ClientSideStreamErrorEvent;
}

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
