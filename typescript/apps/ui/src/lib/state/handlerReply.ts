/**
 * Whether a handler's reply is prose or data, decided once for every surface that shows it.
 *
 * The chat bubble, the transcript, the log's "Handler reply" row and the graph's Output node
 * each used to guess from the payload — a string `text` meant prose — so a model carrying
 * `text` beside its records showed only the sentence, and `{text: ""}` showed nothing. The
 * handler already declares what it returns: `agent_interface` carries each handler's output
 * schema. Prose is exactly the free-text shape, an object whose only field is `text`
 * (`TextReply`) or the legacy `message`. Everything else is data and is shown whole.
 */
import type { AgentInterfaceFunction, AgentSseFrame } from "$lib/api/types";
import { messageKey } from "$lib/state/inboundMessageText";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const onlyProseKey = (keys: string[]): boolean =>
  keys.length === 1 && (keys[0] === "text" || keys[0] === "message");

/** True for the free-text output shape, false for any other declared shape, undefined with no schema. */
export function proseSchema(schema: unknown): boolean | undefined {
  if (!isRecord(schema)) return undefined;
  return isRecord(schema.properties) && onlyProseKey(Object.keys(schema.properties));
}

export interface HandlerReply {
  /** Prose to render as markdown, or the payload pretty-printed for a surface that shows text. */
  text: string;
  /** The whole payload, present exactly when the reply is data. */
  json?: unknown;
}

/**
 * A finished reply. Without a schema (an old run, a replay with no interface, a subagent
 * whose interface never loaded) the payload's own keys decide, by the same rule.
 */
export function handlerReply(data: { text?: unknown; output?: unknown }, schema?: unknown): HandlerReply {
  if (typeof data.text === "string") return { text: data.text };
  const output = data.output;
  if (typeof output === "string") return { text: output };
  if (typeof output !== "object" || output === null) return { text: "" };
  const keys = Object.keys(output);
  const value = (output as Record<string, unknown>)[keys[0]];
  if (proseSchema(schema) !== false && onlyProseKey(keys) && typeof value === "string") return { text: value };
  return { text: JSON.stringify(output, null, 2), json: output };
}

const FENCE_OPEN = /^\s*```(?:json)?[ \t]*\n/;
const FENCE_CLOSE = /\n?```\s*$/;

/** Streamed JSON without the markdown fence a model sometimes wraps it in. */
export function stripJsonFence(text: string): string {
  return FENCE_OPEN.test(text) ? text.replace(FENCE_OPEN, "").replace(FENCE_CLOSE, "") : text.trimStart();
}

/* Strict on purpose: `[1] See the footnote` and `{name} is a placeholder` are prose. A
   bracketed scalar counts only once a comma shows it is a list. */
const JSON_OPENING = /^(?:\{\s*["}]|\[\s*[[{"\]]|\[\s*(?:-?\d[\d.eE+-]*|true|false|null)\s*,)/;

/**
 * A reply still streaming. A handler declared as data streams JSON from its first delta and
 * one declared as prose never does; only with no schema is the opening sniffed.
 */
export function streamingIsData(text: string, schema?: unknown): boolean {
  const prose = proseSchema(schema);
  if (prose !== undefined) return !prose;
  return JSON_OPENING.test(stripJsonFence(text.slice(0, 256)));
}

/**
 * Matches reply frames to the output schema of the handler they answer. Only
 * message_accepted names the handler, so the frames after it are matched by message —
 * within `scope`, so two agents' messages never share an entry.
 */
export function outputSchemaTracker(): (
  frame: AgentSseFrame,
  agentInterface: AgentInterfaceFunction[] | undefined,
  scope?: string
) => unknown {
  const handlers = new Map<string, string>();
  return (frame, agentInterface, scope = "") => {
    if (!("type" in frame.data)) return undefined;
    const key = `${scope}\u0000${messageKey(frame)}`;
    if (frame.event === "message_accepted") handlers.set(key, frame.data.handler);
    const handler = handlers.get(key);
    return handler === undefined ? undefined : agentInterface?.find((fn) => fn.name === handler)?.output;
  };
}
