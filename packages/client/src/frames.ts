// The frames `/api/attach` streams as SSE: each event's generated protocol payload, flattened
// together with the envelope fields `web/app.py` adds to it. A field added to an event in
// `events.py` reaches these types by regenerating `protocol.ts`. The only hand-written parts
// are what the wire leaves open: the envelope fields below, the payload fields narrowed in
// `Narrowed`, and `stream_error`, which no agent publishes.

import type * as Protocol from "./protocol.ts";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonRecord = Record<string, unknown>;

export type AgentEventType = Protocol.AgentEventType;
export type TokenUsage = Protocol.TokenUsage;
export type MessageDisposition = Protocol.MessageDisposition;
export type AutoApprovalVerdict = Protocol.AutoApprovalVerdict;

export interface AgentEventMetadata extends Omit<Protocol.AgentEvent, "event"> {
  /** The root-stream position to resume from: pass it back as `from_offset`. Several
   *  subagent frames can share one, so it is a cursor, not an identity. */
  resume_offset: number;
  /** The event's own offset in its agent's log, when the server reports one. */
  event_offset?: number;
  /** Present, and true, when the event was already durable as the stream opened. */
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
 * One RFC 6902 operation off an agent's observable state. The harness emits only these
 * three kinds, never `move`, `copy` or `test`; the applier still refuses anything else.
 */
export interface JsonPatchOp {
  op: "add" | "replace" | "remove";
  /** RFC 6901 pointer. `""` is the whole document; a trailing `/-` appends. */
  path: string;
  /** Absent on `remove`, and only on `remove`. */
  value?: JsonValue;
}

/**
 * Synthesized by `POST /api/chat` for a timeout, or for that turn's own
 * `message_handler_error`. Not an agent event: nothing published it on the stream, so it
 * carries no type or envelope.
 */
export interface ClientSideStreamErrorEvent {
  kind: "timeout" | "agent";
  message: string;
  resume_offset: number;
}

export type AgentSseEventMap = { [TType in AgentEventType]: AgentEventData<TType> } & {
  stream_error: ClientSideStreamErrorEvent;
};

export type AgentStreamEventData = AgentSseEventMap[keyof AgentSseEventMap];

export type AgentSseFrame = {
  [TType in keyof AgentSseEventMap]: { event: TType; data: AgentSseEventMap[TType] };
}[keyof AgentSseEventMap];

/** A frame published by an agent, as opposed to a synthesized `stream_error`. */
export type AgentEventFrame = Exclude<AgentSseFrame, { event: "stream_error" }>;

export function isAgentEventFrame(frame: AgentSseFrame): frame is AgentEventFrame {
  return frame.event !== "stream_error";
}
