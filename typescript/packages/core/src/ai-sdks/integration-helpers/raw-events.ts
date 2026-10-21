/// <reference path="../../sandbox.d.ts" />
// Raw model stream events, translated inside the agent workflow. `@temporalio/ai-sdk`,
// `@temporalio/openai-agents` and the Gemini add-on publish each model call's raw events to a
// topic of the agent workflow's own stream. The harness names that topic after the turn the call
// belongs to, and the runner's publish handler turns the raw parts into harness events on
// turn_events, so readers (in either language) need nothing and nothing runs inside the model
// activity.
//
// A retried model activity publishes its stream again. The worker's harness interceptor stamps
// each attempt onto the topic (`stampAttempt`), so a superseded attempt's late events are dropped
// and its model bracket closed; a model call that fails for good is marked abandoned by the
// workflow, which closes its bracket too. An attempt's deltas already published stay: the turn's
// final reply replaces them (the wire has no event to retract a delta).
// Workflow- and browser-safe: no SDK imports, structural types only.

import type { AgentEvent, AgentStreamItem, TokenUsage, TurnStreamContext } from "../../harness/agent-protocol/index.ts";

const RAW_TOPIC_PREFIX = "turn_events.raw.";

/** `gemini` is the Interactions API's SSE events; `gemini-content` is `generateContentStream` chunks. */
export type RawFormat = "ai-sdk" | "openai-agents" | "gemini" | "gemini-content";
const RAW_FORMATS: readonly RawFormat[] = ["ai-sdk", "openai-agents", "gemini", "gemini-content"];

interface RawTopic {
  format: RawFormat;
  /** The topic without its attempt stamp: one model call. */
  call: string;
  /** The activity and attempt that published it; null on the unstamped call topic. */
  stamp: { activity: string; attempt: number } | null;
  agentId: string;
  turnId: string;
  turnNumber: number;
  messageId: string | null;
  model: string | null;
}

/** The topic model call `call` (unique within the run) streams its raw events to, carrying its turn. */
export function rawEventsTopic(
  format: RawFormat,
  ctx: TurnStreamContext,
  model: string | null,
  call: number,
): string {
  return `${RAW_TOPIC_PREFIX}${format}.${JSON.stringify([ctx.agent_id, ctx.turn_id, ctx.turn_number, ctx.message_id, model, call])}`;
}

/** The workflow publishes this on a model call's topic when the call fails for good. */
export const MODEL_CALL_ABANDONED = { harness_model_call_abandoned: true } as const;

export function parseRawTopic(topic: string): RawTopic | null {
  if (!topic.startsWith(RAW_TOPIC_PREFIX)) return null;
  const rest = topic.slice(RAW_TOPIC_PREFIX.length);
  const dot = rest.indexOf(".");
  const format = rest.slice(0, dot) as RawFormat;
  if (dot < 0 || !RAW_FORMATS.includes(format)) return null;
  const end = rest.lastIndexOf("]");
  const stamp = /^(?:~(\d+)~([\w-]*))?$/.exec(rest.slice(end + 1));
  if (stamp === null) return null;
  try {
    const [agentId, turnId, turnNumber, messageId, model] = JSON.parse(
      rest.slice(dot + 1, end + 1),
    ) as [string, string, number, string | null, string | null];
    return {
      format,
      call: topic.slice(0, RAW_TOPIC_PREFIX.length + end + 1),
      stamp: stamp[1] === undefined ? null : { attempt: Number(stamp[1]), activity: stamp[2]! },
      agentId,
      turnId,
      turnNumber,
      messageId,
      model,
    };
  } catch {
    return null;
  }
}

/**
 * `arg` with any raw topic in it stamped with the publishing activity and attempt: a topic
 * string, or an object's `streamingTopic` (how `@temporalio/ai-sdk` and `@temporalio/openai-agents`
 * take it). One model call can run several activities (an OpenAI Agents run), hence the id.
 */
export function stampAttempt(arg: unknown, activityId: string, attempt: number): unknown {
  const stamp = (topic: string) =>
    parseRawTopic(topic)?.stamp === null
      ? `${topic}~${attempt}~${activityId.replace(/[^\w-]/g, "_")}`
      : topic;
  if (typeof arg === "string") return stamp(arg);
  if (isObject(arg) && typeof arg.streamingTopic === "string")
    return { ...arg, streamingTopic: stamp(arg.streamingTopic) };
  return arg;
}

type Json = Record<string, unknown>;
const isObject = (v: unknown): v is Json =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" ? v : null);

function parseArgs(raw: unknown): Json {
  if (isObject(raw)) return raw;
  try {
    const parsed: unknown = JSON.parse(typeof raw === "string" && raw ? raw : "{}");
    return isObject(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

// -- AI SDK: LanguageModelV4StreamPart, published as JSON bytes ---------------------------------

function aiSdkUsage(u: unknown): TokenUsage | null {
  if (!isObject(u)) return null;
  const input = isObject(u.inputTokens) ? u.inputTokens : {};
  const output = isObject(u.outputTokens) ? u.outputTokens : {};
  const i = num(input.total);
  const o = num(output.total);
  return {
    input_tokens: i,
    output_tokens: o,
    total_tokens: i !== null && o !== null ? i + o : null,
    thought_tokens: num(output.reasoning),
    cached_tokens: num(input.cacheRead),
    tool_use_tokens: null,
  };
}

function fromAiSdk(part: Json, model: string | null): AgentStreamItem[] {
  switch (part.type) {
    case "stream-start":
      return [{ type: "model_interaction_started", model }];
    case "text-delta":
      return part.delta ? [{ type: "reply_delta", text: String(part.delta) }] : [];
    case "reasoning-delta":
      return part.delta ? [{ type: "thought_summary", delta: { delta: part.delta } }] : [];
    case "source":
      return [{ type: "text_annotation", delta: { annotations: [part] } }];
    case "tool-call":
      return [
        {
          type: "tool_requested",
          tool_id: String(part.toolCallId),
          tool_name: String(part.toolName),
          tool_input: parseArgs(part.input),
        },
      ];
    case "finish":
      return [{ type: "model_interaction_ended", model, usage: aiSdkUsage(part.usage) }];
    case "error":
      return [{ type: "model_interaction_ended", model, usage: null }];
    default:
      return [];
  }
}

// -- OpenAI Agents: agents-core StreamEvent ------------------------------------------------------

function openAIUsage(u: unknown): TokenUsage | null {
  if (!isObject(u)) return null;
  const first = (d: unknown): Json =>
    Array.isArray(d) ? (isObject(d[0]) ? d[0] : {}) : isObject(d) ? d : {};
  return {
    input_tokens: num(u.inputTokens),
    output_tokens: num(u.outputTokens),
    total_tokens: num(u.totalTokens),
    thought_tokens: num(first(u.outputTokensDetails).reasoning_tokens),
    cached_tokens: num(first(u.inputTokensDetails).cached_tokens),
    tool_use_tokens: null,
  };
}

function fromOpenAI(event: Json, model: string | null): AgentStreamItem[] {
  switch (event.type) {
    case "response_started":
      return [{ type: "model_interaction_started", model }];
    case "output_text_delta":
      return event.delta ? [{ type: "reply_delta", text: String(event.delta) }] : [];
    case "model": {
      const raw = isObject(event.event) ? event.event : {};
      if (raw.type === "response.reasoning_summary_text.delta")
        return [{ type: "thought_summary", delta: raw }];
      if (raw.type === "response.output_text.annotation.added")
        return [{ type: "text_annotation", delta: raw }];
      return [];
    }
    case "response_done": {
      const response = isObject(event.response) ? event.response : {};
      const output = Array.isArray(response.output) ? response.output.filter(isObject) : [];
      return [
        ...output
          .filter((item) => item.type === "function_call")
          .map((item): AgentStreamItem => ({
            type: "tool_requested",
            tool_id: String(item.callId),
            tool_name: String(item.name),
            tool_input: parseArgs(item.arguments),
          })),
        { type: "model_interaction_ended", model, usage: openAIUsage(response.usage) },
      ];
    }
    default:
      return [];
  }
}

// -- Gemini: Interactions SSE events, keyed by step index ----------------------------------------

function geminiUsage(usage: unknown): TokenUsage | null {
  if (!isObject(usage)) return null;
  return {
    input_tokens: num(usage.total_input_tokens),
    output_tokens: num(usage.total_output_tokens),
    thought_tokens: num(usage.total_thought_tokens),
    cached_tokens: num(usage.total_cached_tokens),
    tool_use_tokens: num(usage.total_tool_use_tokens),
    total_tokens: num(usage.total_tokens),
  };
}

class GeminiSteps {
  readonly searchCalls = new Map<number, string>();
  readonly searchResults = new Map<number, string>();
  readonly calls = new Map<number, { id: string; name: string; args: Json; buffer: string }>();

  translate(event: Json, model: string | null): AgentStreamItem[] {
    const index = num(event.index) ?? -1;
    switch (event.event_type) {
      case "interaction.created":
        return [{ type: "model_interaction_started", model }];
      case "interaction.completed":
        return [
          {
            type: "model_interaction_ended",
            model,
            usage: geminiUsage(isObject(event.interaction) ? event.interaction.usage : undefined),
          },
        ];
      case "error":
        return [{ type: "model_interaction_ended", model, usage: null }];
      case "step.start": {
        const step = isObject(event.step) ? event.step : {};
        if (step.type === "file_search_call") this.searchCalls.set(index, String(step.id ?? ""));
        else if (step.type === "file_search_result")
          this.searchResults.set(index, String(step.call_id ?? ""));
        else if (step.type === "function_call")
          this.calls.set(index, {
            id: String(step.id ?? ""),
            name: String(step.name ?? ""),
            args: parseArgs(step.arguments),
            buffer: "",
          });
        return [];
      }
      case "step.delta": {
        const delta = isObject(event.delta) ? event.delta : {};
        if (delta.type === "text")
          return delta.text ? [{ type: "reply_delta", text: String(delta.text) }] : [];
        if (delta.type === "thought_summary") return [{ type: "thought_summary", delta }];
        if (delta.type === "text_annotation_delta") return [{ type: "text_annotation", delta }];
        const call = this.calls.get(index);
        if (delta.type === "arguments_delta" && call !== undefined)
          call.buffer += String(delta.arguments ?? "");
        return [];
      }
      case "step.stop": {
        const search = this.searchCalls.get(index);
        if (this.searchCalls.delete(index))
          return [
            { type: "tool_start", tool_id: search!, tool_name: "file_search", tool_input: {} },
          ];
        const result = this.searchResults.get(index);
        if (this.searchResults.delete(index))
          return [
            { type: "tool_end", tool_id: result!, tool_name: "file_search", tool_output: "" },
          ];
        const call = this.calls.get(index);
        if (call === undefined) return [];
        this.calls.delete(index);
        return [
          {
            type: "tool_requested",
            tool_id: call.id,
            tool_name: call.name,
            tool_input: call.buffer ? parseArgs(call.buffer) : call.args,
          },
        ];
      }
      default:
        return [];
    }
  }
}

// -- Gemini generateContentStream: one GenerateContentResponse per chunk --------------------------

function fromGeminiContent(chunk: Json): AgentStreamItem[] {
  const candidate: unknown = Array.isArray(chunk.candidates) ? chunk.candidates[0] : undefined;
  const parts: unknown =
    isObject(candidate) && isObject(candidate.content) ? candidate.content.parts : undefined;
  if (!Array.isArray(parts)) return [];
  const text = parts
    .map((p) => (isObject(p) && p.thought !== true && typeof p.text === "string" ? p.text : ""))
    .join("");
  return text ? [{ type: "reply_delta", text }] : [];
}

/** Turns one agent stream's raw-topic items into harness envelopes; one per agent workflow. */
export class RawEventTranslator {
  readonly #gemini = new Map<string, GeminiSteps>();
  // The current attempt per publishing activity; per model call, whether its bracket is open
  // (unknown when no start was seen, which then closes it to be safe) and whether the workflow
  // abandoned it.
  readonly #attempt = new Map<string, number>();
  readonly #open = new Map<string, boolean>();
  readonly #abandoned = new Set<string>();
  readonly #structured = new Set<string>();

  /** Drop model call `call`'s reply deltas: its text is a structured-output document. */
  structuredOutput(call: string): void {
    this.#structured.add(call);
  }

  translate(topic: string, data: unknown): AgentEvent[] {
    const raw = parseRawTopic(topic);
    if (raw === null) return [];
    const decoded: unknown =
      data instanceof Uint8Array ? safeJson(new TextDecoder().decode(data)) : data;
    if (!isObject(decoded)) return [];
    if (this.#abandoned.has(raw.call)) return [];
    const events: AgentStreamItem[] = [];
    const close = () => {
      if (this.#open.get(raw.call) !== false)
        events.push({ type: "model_interaction_ended", model: raw.model, usage: null });
      this.#open.set(raw.call, false);
    };
    if (decoded.harness_model_call_abandoned === true) {
      close();
      this.#abandoned.add(raw.call);
      return this.#envelopes(raw, events);
    }
    const unit = `${raw.call}~${raw.stamp?.activity ?? ""}`;
    const attempt = raw.stamp?.attempt ?? 1;
    const reading = this.#attempt.get(unit) ?? 0;
    if (attempt < reading) return [];
    if (attempt > reading) {
      if (reading > 0) close();
      this.#attempt.set(unit, attempt);
    }
    const translated = this.#translate(topic, raw, decoded);
    events.push(
      ...(this.#structured.has(raw.call)
        ? translated.filter((e) => e.type !== "reply_delta")
        : translated),
    );
    for (const e of events) {
      if (e.type === "model_interaction_started") this.#open.set(raw.call, true);
      if (e.type === "model_interaction_ended") this.#open.set(raw.call, false);
    }
    return this.#envelopes(raw, events);
  }

  #envelopes(raw: RawTopic, events: AgentStreamItem[]): AgentEvent[] {
    const timestamp = Date.now() / 1000;
    return events.map((event) => ({
      agent_id: raw.agentId,
      turn_id: raw.turnId,
      turn_number: raw.turnNumber,
      message_id: raw.messageId,
      timestamp,
      event,
    }));
  }

  #translate(topic: string, raw: RawTopic, decoded: Json): AgentStreamItem[] {
    let events: AgentStreamItem[];
    switch (raw.format) {
      case "ai-sdk":
        events = fromAiSdk(decoded, raw.model);
        break;
      case "openai-agents":
        events = fromOpenAI(decoded, raw.model);
        break;
      case "gemini": {
        let steps = this.#gemini.get(topic);
        if (steps === undefined) this.#gemini.set(topic, (steps = new GeminiSteps()));
        events = steps.translate(decoded, raw.model);
        if (decoded.event_type === "interaction.completed" || decoded.event_type === "error")
          this.#gemini.delete(topic);
        break;
      }
      case "gemini-content":
        events = fromGeminiContent(decoded);
        break;
      default: {
        const unknownFormat: never = raw.format;
        throw new Error(`unknown raw event format ${String(unknownFormat)}`);
      }
    }
    return events;
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
