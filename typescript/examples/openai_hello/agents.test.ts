// OpenAIHelloAgentTs end to end on a dev server, its model calls answered by a scripted stand-in
// for OpenAI (no network): asked about the weather, the model calls get_weather, the tool runs, and
// the reply streams back before the turn ends.

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { after, before, it } from "node:test";
import { fileURLToPath } from "node:url";
import type {
  Model,
  ModelProvider,
  ModelRequest,
  ModelResponse,
  StreamEvent,
} from "@openai/agents-core";
import type { AgentEvent } from "@temporalio/agent-harness/harness/agent-protocol";
import { AgentHarnessPlugin } from "@temporalio/agent-harness-worker";
import { OpenAIAgentsPlugin } from "@temporalio/agent-harness-worker/ai-sdks/openai-agents";
import { AgentClient } from "@temporalio/agent-harness-worker/harness/agent-client";
import { Client } from "@temporalio/client";
import { TestWorkflowEnvironment } from "@temporalio/testing";
import { NativeConnection, Worker } from "@temporalio/worker";
import { DEFAULT_MODEL } from "./agents.ts";

const AGENTS = fileURLToPath(new URL("./agents.js", import.meta.url));
const CALL_ID = "call_weather_1";
const ARGS = '{"city": "Paris"}';
const REPLY = "It's 72°F and sunny in Paris.";

type Output = Extract<StreamEvent, { type: "response_done" }>["response"]["output"];

/** A raw Responses event, as OpenAIResponsesModel streams it beside its normalized ones. */
const raw = (event: Record<string, unknown>): StreamEvent => ({
  type: "model",
  event,
  providerData: { rawModelEventSource: "openai-responses" },
});

const responseDone = (output: Output): StreamEvent => ({
  type: "response_done",
  response: {
    id: "resp_1",
    usage: {
      inputTokens: 11,
      outputTokens: 22,
      totalTokens: 33,
      inputTokensDetails: {},
      outputTokensDetails: {},
    },
    output,
  },
});

/** The first call asks for get_weather({city: Paris}); once its result is in, the reply. */
function weatherTurn(request: ModelRequest): StreamEvent[] {
  const hasToolResult =
    Array.isArray(request.input) &&
    request.input.some((item) => item.type === "function_call_result");
  if (!hasToolResult)
    return [
      { type: "response_started" },
      raw({
        type: "response.output_item.added",
        output_index: 0,
        item: { type: "function_call", id: "fc_1", call_id: CALL_ID, name: "get_weather" },
      }),
      raw({ type: "response.function_call_arguments.delta", item_id: "fc_1", delta: ARGS }),
      raw({
        type: "response.function_call_arguments.done",
        item_id: "fc_1",
        name: "get_weather",
        arguments: ARGS,
      }),
      responseDone([
        {
          type: "function_call",
          id: "fc_1",
          callId: CALL_ID,
          name: "get_weather",
          arguments: ARGS,
          status: "completed",
        },
      ]),
    ];
  return [
    { type: "response_started" },
    { type: "output_text_delta", delta: "It's 72°F " },
    { type: "output_text_delta", delta: "and sunny in Paris." },
    responseDone([
      {
        type: "message",
        role: "assistant",
        status: "completed",
        content: [{ type: "output_text", text: REPLY }],
      },
    ]),
  ];
}

/** Serves the agent's model, recording each request. */
const requests: ModelRequest[] = [];
const modelProvider: ModelProvider = {
  getModel(name) {
    assert.equal(name, DEFAULT_MODEL);
    const model: Model = {
      async getResponse(): Promise<ModelResponse> {
        throw new Error("the harness only streams");
      },
      async *getStreamedResponse(request) {
        requests.push(request);
        yield* weatherTurn(request);
      },
    };
    return model;
  },
};

let env: TestWorkflowEnvironment;
let connection: NativeConnection;
let worker: Worker;
let running: Promise<void>;
const taskQueue = `test-openai-hello-${randomUUID()}`;

before(async () => {
  const cli = process.env.TEMPORAL_CLI_PATH;
  if (!cli) throw new Error("TEMPORAL_CLI_PATH is not set; run the test with Bazel");
  env = await TestWorkflowEnvironment.createLocal({
    server: { executable: { type: "existing-path", path: resolve(cli) } },
  });
  connection = await NativeConnection.connect({ address: env.address });
  // As worker.ts configures it, with the scripted model in place of OpenAI.
  worker = await Worker.create({
    connection,
    namespace: env.namespace,
    taskQueue,
    workflowsPath: AGENTS,
    plugins: [
      new OpenAIAgentsPlugin({ modelProvider }),
      new AgentHarnessPlugin({ largePayloadOffload: null }),
    ],
  });
  running = worker.run();
});

after(async () => {
  worker.shutdown();
  await running;
  await connection.close();
  await env.teardown();
});

it("answers a weather question with a get_weather call and a streamed reply", async () => {
  const client: Client = env.client;
  const workflowId = `openai-hello-${randomUUID()}`;
  await client.workflow.start("OpenAIHelloAgentTs", { taskQueue, workflowId, args: [{}] });
  const agent = new AgentClient(client, workflowId);

  const events: AgentEvent["event"][] = [];
  for await (const [item] of await agent.sendMessage("ask", {
    text: "What's the weather in Paris?",
  })) {
    if (item instanceof Error) throw item;
    events.push(item.event);
  }
  await client.workflow.getHandle(workflowId).terminate("test finished");

  assert.deepStrictEqual(
    events.map((e) => e.type),
    [
      "turn_started",
      "message_handler_start",
      "model_interaction_started",
      "tool_requested",
      "model_interaction_ended",
      "tool_start",
      "tool_end",
      "model_interaction_started",
      "reply_delta",
      "reply_delta",
      "model_interaction_ended",
      "message_handler_end",
      "turn_end",
    ],
  );
  const ofType = (type: string) => events.filter((e) => e.type === type);
  assert.deepStrictEqual(ofType("tool_requested"), [
    {
      type: "tool_requested",
      tool_id: CALL_ID,
      tool_name: "get_weather",
      tool_input: { city: "Paris" },
    },
  ]);
  assert.deepStrictEqual(ofType("tool_end"), [
    {
      type: "tool_end",
      tool_id: CALL_ID,
      tool_name: "get_weather",
      tool_output: "It's 72°F and sunny in Paris.",
    },
  ]);
  assert.equal(ofType("reply_delta").map((e) => (e as { text: string }).text).join(""), REPLY);
  assert.deepStrictEqual(ofType("message_handler_end"), [
    { type: "message_handler_end", output: { text: REPLY } },
  ]);
  // The model got the instructions and the tool, then the tool's result in the second call.
  assert.equal(requests.length, 2);
  assert.match(requests[0]!.systemInstructions ?? "", /one tool, `get_weather`/);
  assert.deepStrictEqual(
    requests[0]!.tools.map((t) => (t as { name: string }).name),
    ["get_weather"],
  );
});
