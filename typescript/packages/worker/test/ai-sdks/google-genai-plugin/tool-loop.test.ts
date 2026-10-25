// An agent running the Gemini Interactions tool loop on a dev server, against a scripted client
// in GoogleGenAIPlugin: the model calls run in the plugin's activity, which publishes their events
// to the turn, and the function call runs as a harness tool between them.

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { after, before, describe, it } from "node:test";
import { Client, Connection } from "@temporalio/client";
import { NativeConnection, Worker } from "@temporalio/worker";
import { GoogleGenAIPlugin } from "../../../src/ai-sdks/google-genai-plugin/index.ts";
import { AgentClient } from "../../../src/harness/agent-client.ts";
import { AgentHarnessPlugin, toolActivity } from "../../../src/index.ts";
import { assertMatch } from "../../match.ts";
import { collect, one, send, startServer, typesFor, type TemporalServer } from "../../support.ts";
import { MODEL, getWeather } from "./agents.ts";
import { ScriptedGemini, call, completed, created, text, thought } from "./script.ts";

const AGENTS = fileURLToPath(new URL("./agents.js", import.meta.url));

const usage = (total: number) => ({
  total_input_tokens: total - 1,
  total_output_tokens: 1,
  total_tokens: total,
});
const tokenUsage = (total: number) => ({
  input_tokens: total - 1,
  output_tokens: 1,
  thought_tokens: null,
  cached_tokens: null,
  tool_use_tokens: null,
  total_tokens: total,
});

describe("a Gemini Interactions tool loop", () => {
  let server: TemporalServer;
  let nativeConnection: NativeConnection;
  let connection: Connection;
  let client: Client;
  let worker: Worker;
  let running: Promise<void>;
  const taskQueue = `gemini-tool-loop-${randomUUID()}`;
  const gemini = new ScriptedGemini([
    [
      created("int-1"),
      ...thought(0, "t-sig"),
      ...text(1, "Let me ", "check."),
      ...call(2, "call_1", "get_weather", '{"city": "Paris"}'),
      completed("int-1", usage(10)),
    ],
    [created("int-2"), ...text(0, "It is sunny ", "in Paris."), completed("int-2", usage(20))],
    // The tests run in order, so these answer the callback test's two model calls.
    [
      created("int-3"),
      ...call(0, "call_ask", "ask_user", '{"question": "Which city?"}'),
      completed("int-3", usage(30)),
    ],
    [created("int-4"), ...text(0, "Lisbon it ", "is."), completed("int-4", usage(40))],
  ]);

  before(async () => {
    server = await startServer();
    nativeConnection = await NativeConnection.connect({ address: server.address });
    worker = await Worker.create({
      connection: nativeConnection,
      namespace: server.namespace,
      taskQueue,
      workflowsPath: AGENTS,
      plugins: [
        new GoogleGenAIPlugin(gemini),
        new AgentHarnessPlugin({
          tools: [toolActivity(getWeather, async ({ city }) => `sunny in ${city}`)],
        }),
      ],
    });
    running = worker.run();
    connection = await Connection.connect({ address: server.address });
    client = new Client({ connection, namespace: server.namespace });
  });
  after(async () => {
    worker.shutdown();
    await running;
    await nativeConnection.close();
    await connection.close();
    await server.teardown();
  });

  it("publishes each model call's events around the harness tool's lifecycle", async () => {
    const workflowId = `gemini-${randomUUID()}`;
    const handle = await client.workflow.start("GeminiToolLoopAgent", {
      taskQueue,
      workflowId,
      args: [{}],
    });
    try {
      await send(handle, "ask", { text: "Weather in Paris?" });
      const events = (await collect(client, workflowId)).map((e) => e.event);

      assert.deepStrictEqual(events, [
        {
          type: "message_accepted",
          handler: "ask",
          payload: { text: "Weather in Paris?" },
          disposition: "opened",
        },
        { type: "turn_started" },
        { type: "message_handler_start" },
        { type: "model_interaction_started", model: MODEL },
        { type: "reply_delta", text: "Let me " },
        { type: "reply_delta", text: "check." },
        {
          type: "tool_requested",
          tool_id: "call_1",
          tool_name: "get_weather",
          tool_input: { city: "Paris" },
        },
        { type: "model_interaction_ended", model: MODEL, usage: tokenUsage(10) },
        {
          type: "tool_start",
          tool_id: "call_1",
          tool_name: "get_weather",
          tool_input: { city: "Paris" },
        },
        {
          type: "tool_end",
          tool_id: "call_1",
          tool_name: "get_weather",
          tool_output: "sunny in Paris",
        },
        { type: "model_interaction_started", model: MODEL },
        { type: "reply_delta", text: "It is sunny " },
        { type: "reply_delta", text: "in Paris." },
        { type: "model_interaction_ended", model: MODEL, usage: tokenUsage(20) },
        { type: "message_handler_end", output: { text: "It is sunny in Paris." } },
        { type: "turn_end" },
      ]);

      // The second call carried the whole conversation, the function result included.
      assert.equal(gemini.requests.length, 2);
      assert.deepStrictEqual(gemini.requests[1]!.params.input, [
        { type: "user_input", content: [{ type: "text", text: "Weather in Paris?" }] },
        { type: "thought", signature: "t-sig" },
        { type: "model_output", content: [{ type: "text", text: "Let me check." }] },
        {
          type: "function_call",
          id: "call_1",
          name: "get_weather",
          arguments: { city: "Paris" },
          signature: "sig-call_1",
        },
        {
          type: "function_result",
          call_id: "call_1",
          name: "get_weather",
          result: "sunny in Paris",
          signature: "sig-call_1",
        },
      ]);
      assert.deepStrictEqual(gemini.requests[0]!.params.tools, [
        {
          type: "function",
          name: "get_weather",
          description: "The current weather in a city.",
          parameters: getWeather.parameters,
        },
      ]);
    } finally {
      await handle.terminate("test finished").catch(() => undefined);
    }
  });

  it("parks a callback tool's call until the client answers, then hands the answer to the model", async () => {
    const workflowId = `gemini-ask-${randomUUID()}`;
    const handle = await client.workflow.start("GeminiAskUserAgent", {
      taskQueue,
      workflowId,
      args: [{}],
    });
    try {
      await send(handle, "ask", { text: "Plan my trip." });
      const events = await collect(client, workflowId, {
        onEvent: async (e) => {
          if (e.event.type === "callback_requested")
            await new AgentClient(client, workflowId).provideCallbackResult(e.event.tool_id, {
              result: "Lisbon",
            });
        },
      });

      assert.deepStrictEqual(typesFor(events, "call_ask"), [
        "tool_requested",
        "tool_start",
        "callback_requested",
        "callback_resolved",
        "tool_end",
      ]);
      assertMatch(one(events, "callback_requested"), {
        tool_name: "ask_user",
        tool_input: { question: "Which city?" },
      });
      assert.equal(one(events, "message_handler_end").output.text, "Lisbon it is.");
      // The model's next call carried the user's answer as the function result.
      assertMatch(gemini.requests[3]!.params.input.at(-1), {
        type: "function_result",
        call_id: "call_ask",
        result: "Lisbon",
      });
    } finally {
      await handle.terminate("test finished").catch(() => undefined);
    }
  });
});
