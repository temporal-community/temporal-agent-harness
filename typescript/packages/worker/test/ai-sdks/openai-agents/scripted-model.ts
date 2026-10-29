// A scripted stand-in for OpenAI: agents-core stream events shaped as OpenAIResponsesModel emits
// them (normalized `output_text_delta` / `response_done`, raw Responses events as `model` events),
// and a ModelProvider whose model asks for get_weather, then answers once the tool has run.

import type {
  Model,
  ModelProvider,
  ModelRequest,
  ModelResponse,
  StreamEvent,
} from "@openai/agents-core";

const raw = (event: Record<string, unknown>): StreamEvent => ({
  type: "model",
  event,
  providerData: { rawModelEventSource: "openai-responses" },
});

export const textDelta = (delta: string): StreamEvent => ({ type: "output_text_delta", delta });

export const reasoningDelta = (delta: string): StreamEvent =>
  raw({ type: "response.reasoning_summary_text.delta", delta, item_id: "rs_1", logprobs: null });

export const annotationAdded = (): StreamEvent =>
  raw({
    type: "response.output_text.annotation.added",
    annotation: { type: "url_citation", url: "https://example.com", title: null },
    item_id: "msg_1",
  });

export const fnCallAdded = (itemId: string, callId: string, name: string): StreamEvent =>
  raw({
    type: "response.output_item.added",
    output_index: 0,
    item: { type: "function_call", id: itemId, call_id: callId, name, arguments: "" },
  });

export const argsDelta = (itemId: string, delta: string): StreamEvent =>
  raw({ type: "response.function_call_arguments.delta", item_id: itemId, delta });

export const argsDone = (itemId: string, name: string, args: string): StreamEvent =>
  raw({ type: "response.function_call_arguments.done", item_id: itemId, name, arguments: args });

type Output = Extract<StreamEvent, { type: "response_done" }>["response"]["output"];

export const responseDone = (output: Output = []): StreamEvent => ({
  type: "response_done",
  response: {
    id: "resp_1",
    usage: {
      inputTokens: 11,
      outputTokens: 22,
      totalTokens: 33,
      inputTokensDetails: { cached_tokens: 5 },
      outputTokensDetails: { reasoning_tokens: 7 },
    },
    output,
  },
});

export const functionCallItem = (callId: string, name: string, args: string): Output[number] => ({
  type: "function_call",
  id: `fc_${callId}`,
  callId,
  name,
  arguments: args,
  status: "completed",
});

const messageItem = (text: string): Output[number] => ({
  type: "message",
  role: "assistant",
  status: "completed",
  content: [{ type: "output_text", text }],
});

/** The model the weather agents ask for. */
export const SCRIPTED_MODEL = "scripted-model";
/** The weather agents' own `defaultModelParams.startToCloseTimeout`. */
export const WORKFLOW_MODEL_TIMEOUT = "37 seconds";
/** The model the MCP agent asks for. */
export const SCRIPTED_MCP_MODEL = "scripted-mcp-model";
/** The name of the MCP agent's server, and of the worker's provider for it. */
export const PROBE_MCP_SERVER = "probe-mcp";

export const WEATHER_CALL_ID = "call_weather_1";
export const WEATHER_REPLY = "It's sunny in Paris.";
export const MCP_CALL_ID = "call_lookup_1";
export const MCP_REPLY = "Found cats.";

type Script = (request: ModelRequest) => StreamEvent[];

const hasToolResult = (request: ModelRequest) =>
  Array.isArray(request.input) &&
  request.input.some((item) => item.type === "function_call_result");

/**
 * A two-call turn: the model asks for `name(args)`, streaming its arguments in two fragments, then
 * answers `reply` in two text deltas once the tool's result is in its input.
 */
function toolThenReply(callId: string, name: string, args: string, reply: string): Script {
  return (request) => {
    if (!hasToolResult(request)) {
      const cut = Math.ceil(args.length / 2);
      return [
        { type: "response_started" },
        fnCallAdded("fc_1", callId, name),
        argsDelta("fc_1", args.slice(0, cut)),
        argsDelta("fc_1", args.slice(cut)),
        argsDone("fc_1", name, args),
        responseDone([functionCallItem(callId, name, args)]),
      ];
    }
    const cut = reply.indexOf(" ") + 1;
    return [
      { type: "response_started" },
      textDelta(reply.slice(0, cut)),
      textDelta(reply.slice(cut)),
      responseDone([messageItem(reply)]),
    ];
  };
}

/** get_weather({city: Paris}), then the reply. */
export const weatherScript = toolThenReply(
  WEATHER_CALL_ID,
  "get_weather",
  '{"city": "Paris"}',
  WEATHER_REPLY,
);

/** The MCP server's lookup({q: cats}), then the reply. */
export const mcpScript = toolThenReply(MCP_CALL_ID, "lookup", '{"q": "cats"}', MCP_REPLY);

/** The model the ask-user agent asks for. */
export const SCRIPTED_ASK_MODEL = "scripted-ask-model";
export const ASK_CALL_ID = "call_ask_1";
export const ASK_REPLY = "Lisbon it is.";

/** ask_user({question: Which city?}), then the reply. */
export const askScript = toolThenReply(
  ASK_CALL_ID,
  "ask_user",
  '{"question": "Which city?"}',
  ASK_REPLY,
);

/** The model the failing-MCP agent asks for. */
export const SCRIPTED_MCP_ERROR_MODEL = "scripted-mcp-error-model";
/** A term the probe MCP server fails to look up, reporting it in the result. */
export const UNKNOWN_TERM = "boom";
export const MCP_ERROR_CALL_ID = "call_lookup_boom";
export const MCP_ERROR_REPLY = "Nothing found.";

/** lookup({q: boom}), which the server reports as an error, then the reply. */
export const mcpErrorScript = toolThenReply(
  MCP_ERROR_CALL_ID,
  "lookup",
  `{"q": "${UNKNOWN_TERM}"}`,
  MCP_ERROR_REPLY,
);

/** A provider whose model `name` streams `scripts[name](request)`, recording each request. */
export function scriptedProvider(
  scripts: Readonly<Record<string, Script>>,
): ModelProvider & { requests: ModelRequest[] } {
  const requests: ModelRequest[] = [];
  return {
    requests,
    getModel(name) {
      const script = scripts[name ?? ""];
      if (script === undefined) throw new Error(`no script for model ${name}`);
      const model: Model = {
        async getResponse(): Promise<ModelResponse> {
          throw new Error("the harness only streams");
        },
        async *getStreamedResponse(request) {
          requests.push(request);
          yield* script(request);
        },
      };
      return model;
    },
  };
}
