/**
 * How a message the operator sent is shown back to them.
 *
 * Two directions, which is why both live here. displayTextForMessage() takes
 * the structured message about to be submitted; renderUserMessage() takes the
 * `handler` + `payload` a `message_accepted` frame carries back. They agree by
 * sharing one rule rather than by both getting it right, and that rule is
 * deliberately generic: handler names and payload shapes are agent-specific, so
 * a lone string field is the common prompt shape (whatever it is named —
 * `text`, `script`, `prompt`, …) and anything else is labelled by handler name.
 * No handler is special-cased and no field name is assumed. The server agrees —
 * web/app.py's `_display_user_message` applies the same rule to the message
 * that started a session, so the session list, the chat bubble and the replay
 * log show one string.
 *
 * renderUserMessage() is the only copy. transcript.ts, replayLog.ts,
 * flowProjection.ts and stepTimeline.ts import it rather than each carrying a
 * narrower one; inboundMessageText.test.mjs pins that the private copies have
 * not grown back, because the divergence between three copies was once a bug.
 */
import type {
  AgentInboundMessage,
  AgentMessageObject,
  AgentSseFrame,
  JsonRecord,
  MessageId,
  TurnId
} from "$lib/api/types";

function renderEnvelope(handler: string, payload: unknown): string {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return handler;
  }
  const entries = Object.entries(payload as Record<string, unknown>);
  if (entries.length === 0) return handler;
  if (entries.length === 1 && typeof entries[0][1] === "string") {
    return entries[0][1] as string;
  }
  const rendered = entries
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .sort()
    .join(", ");
  return `${handler}(${rendered})`;
}

/**
 * Render an inbound message — a handler name plus its payload — as a one-line label.
 */
export function renderUserMessage(handler: string, payload: JsonRecord): string {
  return renderEnvelope(handler, payload);
}

/**
 * The key to group a frame's events under: its `message_id` when it has one.
 *
 * Everything a message's dispatch produces — its deltas, its tool calls, its reply — carries
 * that id, and a refcounted turn can hold SEVERAL messages, so `turn_id` is no longer a safe
 * grouping key: two participants would collapse into one bubble. The turn brackets themselves
 * are the only frames with no `message_id`, and they fall back to the turn they close.
 */
export function messageKey(frame: AgentSseFrame): MessageId | TurnId {
  if (!("type" in frame.data)) return "";
  return frame.data.message_id ?? frame.data.turn_id;
}

export function isAgentMessageObject(
  message: AgentInboundMessage
): message is AgentMessageObject {
  return typeof message === "object" && message !== null;
}

export function displayTextForMessage(message: AgentInboundMessage): string {
  if (typeof message === "string") return message.trim();
  const payload = message.payload;
  if (payload && typeof payload === "object") {
    return renderEnvelope(message.type, payload).trim();
  }
  return message.type ? message.type : JSON.stringify(message);
}
