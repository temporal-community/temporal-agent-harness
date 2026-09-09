import type { AgentSseFrame, JsonRecord, MessageId, TurnId } from "$lib/api/types";

/**
 * Render an inbound message — a handler name plus its payload — as a one-line label.
 *
 * Handler names and payload shapes are agent-specific, so this stays generic: a lone string
 * field is the common prompt shape (whatever it is named, `text` / `script` / `prompt` / …),
 * and anything else is labelled by handler name. It never assumes a particular handler exists
 * or that a field is called anything in particular.
 */
export function renderUserMessage(handler: string, payload: JsonRecord): string {
  const entries = Object.entries(payload ?? {});
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
