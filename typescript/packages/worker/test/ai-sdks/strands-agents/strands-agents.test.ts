// The Strands Agents integration end to end: a turn running a harness Strands agent on a dev
// server, its model calls answered by scripted models, publishes the harness event sequence. One
// inline, one activity and one callback tool each run under the model's toolUseId; steering sent
// mid-turn reaches the model with the next tool result; an MCP tool call waits for a person's
// approval, and a denied one never reaches the server; a Strands interrupt is refused. The same
// worker also runs an OpenAI Agents agent, so both integrations' workflow code share one bundle.

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { after, before, describe, it } from "node:test";
import { McpClient, type JSONValue, type McpTransport } from "@strands-agents/sdk";
import { Client, Connection } from "@temporalio/client";
import { NativeConnection, Worker } from "@temporalio/worker";
import type { AgentEvent } from "@temporalio/agent-harness/harness/agent-protocol";
import { AgentClient } from "../../../src/harness/agent-client.ts";
import { OpenAIAgentsPlugin } from "../../../src/ai-sdks/openai-agents/index.ts";
import { StrandsPlugin } from "../../../src/ai-sdks/strands-agents/index.ts";
import { harnessObserverFactory } from "../../../src/ai-sdks/strands-agents-harness.ts";
import { AgentHarnessPlugin, toolActivity } from "../../../src/index.ts";
import { assertMatch } from "../../match.ts";
import { collect, one, send, startServer, typesFor, type TemporalServer } from "../../support.ts";
import {
  SCRIPTED_MODEL as OPENAI_SCRIPTED_MODEL,
  WEATHER_REPLY as OPENAI_WEATHER_REPLY,
  scriptedProvider,
  weatherScript,
} from "../openai-agents/scripted-model.ts";
import { ECHO, SCRIPTED_MODEL_ID, ScriptedModel } from "./scripted-model.ts";
import { ECHO_MCP_SERVER, convertToCelsius } from "./tools.ts";

const WORKFLOWS = fileURLToPath(new URL("./workflows.js", import.meta.url));

const NO_TRANSPORT: McpTransport = {
  async start() {},
  async send() {},
  async close() {},
};

/** An MCP server with one tool, `echo`, that returns its `message`; it records each call. */
class EchoMcpClient extends McpClient {
  static readonly calls: JSONValue[] = [];

  constructor() {
    super({ transport: NO_TRANSPORT });
  }

  override async connect(): Promise<void> {}
  override async disconnect(): Promise<void> {}
  // McpClient's tool type is not exported, so the listing is cast.
  override async listTools(): Promise<never> {
    const inputSchema = {
      type: "object",
      properties: { message: { type: "string" } },
      required: ["message"],
    };
    return [
      { name: "echo", description: "Return the message unchanged.", toolSpec: { inputSchema } },
    ] as never;
  }
  override async callTool(_tool: unknown, args: JSONValue): Promise<JSONValue> {
    EchoMcpClient.calls.push(args);
    const { message } = args as { message: string };
    return { content: [{ type: "text", text: message }], isError: false };
  }
}

describe("a harness Strands agent's turn on a dev server", () => {
  let server: TemporalServer;
  let nativeConnection: NativeConnection;
  let connection: Connection;
  let client: Client;
  let worker: Worker;
  let running: Promise<void>;
  const taskQueue = `test-strands-agents-${randomUUID()}`;
  const started: string[] = [];

  before(async () => {
    server = await startServer();
    nativeConnection = await NativeConnection.connect({ address: server.address });
    worker = await Worker.create({
      connection: nativeConnection,
      namespace: server.namespace,
      taskQueue,
      workflowsPath: WORKFLOWS,
      plugins: [
        new StrandsPlugin({
          models: {
            weather: () =>
              new ScriptedModel([
                { tool: "get_weather", toolUseId: "tu-1", input: { city: "Paris" } },
                { tool: "convert_to_celsius", toolUseId: "tu-2", input: { fahrenheit: 72 } },
                "Sunny and 22.2C in Paris.",
              ]),
            ask: () =>
              new ScriptedModel([
                { tool: "ask_user", toolUseId: "tu-ask", input: { question: "Which city?" } },
                ECHO,
              ]),
            steered: () =>
              new ScriptedModel([
                { tool: "ask_user", toolUseId: "tu-ask", input: { question: "Which city?" } },
                ECHO,
              ]),
            interrupting: () =>
              new ScriptedModel([
                { tool: "get_weather", toolUseId: "tu-1", input: { city: "Paris" } },
                "unreachable",
              ]),
            mcpApproved: () =>
              new ScriptedModel([
                { tool: "echo", toolUseId: "tu-mcp", input: { message: "hello" } },
                ECHO,
              ]),
            mcpDenied: () =>
              new ScriptedModel([
                { tool: "echo", toolUseId: "tu-mcp", input: { message: "hello" } },
                ECHO,
              ]),
          },
          mcpClients: { [ECHO_MCP_SERVER]: () => new EchoMcpClient() },
          observerFactory: harnessObserverFactory,
        }),
        new OpenAIAgentsPlugin({
          modelProvider: scriptedProvider({ [OPENAI_SCRIPTED_MODEL]: weatherScript }),
        }),
        new AgentHarnessPlugin({
          largePayloadOffload: null,
          tools: [toolActivity(
            convertToCelsius,
            async ({ fahrenheit }) => Math.round(((fahrenheit - 32) * 5) / 9 * 10) / 10,
          ),],
        }),
      ],
    });
    running = worker.run();
    connection = await Connection.connect({ address: server.address });
    client = new Client({ connection, namespace: server.namespace });
  });

  after(async () => {
    await Promise.allSettled(
      started.map((id) => client.workflow.getHandle(id).terminate("test finished")),
    );
    worker.shutdown();
    await running;
    await nativeConnection.close();
    await connection.close();
    await server.teardown();
  });

  async function startAgent(workflowType: string) {
    const workflowId = `${workflowType}-${randomUUID()}`;
    const handle = await client.workflow.start(workflowType, { taskQueue, workflowId, args: [{}] });
    started.push(workflowId);
    return handle;
  }

  const types = (events: AgentEvent[]) => events.map((e) => e.event.type);
  const replyOf = (events: AgentEvent[]) =>
    (one(events, "message_handler_end").output as { text: string }).text;
  const byType = (events: AgentEvent[], type: string) =>
    events.filter((e) => e.event.type === type).map((e) => e.event);

  const MODEL_CALL_WITH_TOOL = [
    "model_interaction_started",
    "thought_summary",
    "tool_requested",
    "model_interaction_ended",
  ];
  const MODEL_CALL_WITH_REPLY = [
    "model_interaction_started",
    "thought_summary",
    "reply_delta",
    "model_interaction_ended",
  ];
  const TURN_OPEN = ["message_accepted", "turn_started", "message_handler_start"];
  const TURN_CLOSE = ["message_handler_end", "turn_end"];

  it("streams each model call and runs inline and activity tools under the model's toolUseId", async () => {
    const handle = await startAgent("StrandsWeatherAgent");
    await send(handle, "ask", { text: "Weather in Paris in Celsius?" });
    const events = await collect(client, handle.workflowId);

    assert.deepStrictEqual(types(events), [
      ...TURN_OPEN,
      ...MODEL_CALL_WITH_TOOL,
      "tool_start",
      "tool_end",
      ...MODEL_CALL_WITH_TOOL,
      "tool_start",
      "tool_end",
      ...MODEL_CALL_WITH_REPLY,
      ...TURN_CLOSE,
    ]);
    assert.deepStrictEqual(byType(events, "tool_requested"), [
      {
        type: "tool_requested",
        tool_id: "tu-1",
        tool_name: "get_weather",
        tool_input: { city: "Paris" },
      },
      {
        type: "tool_requested",
        tool_id: "tu-2",
        tool_name: "convert_to_celsius",
        tool_input: { fahrenheit: 72 },
      },
    ]);
    assert.deepStrictEqual(
      byType(events, "tool_end").map((e) => [
        (e as { tool_id: string }).tool_id,
        (e as { tool_output: unknown }).tool_output,
      ]),
      [
        ["tu-1", "It's 72F and sunny in Paris."],
        // The activity tool's body ran on the worker.
        ["tu-2", 22.2],
      ],
    );
    for (const ended of byType(events, "model_interaction_ended"))
      assert.deepStrictEqual(ended, {
        type: "model_interaction_ended",
        model: SCRIPTED_MODEL_ID,
        usage: {
          input_tokens: 10,
          output_tokens: 3,
          total_tokens: 13,
          thought_tokens: null,
          cached_tokens: 4,
          tool_use_tokens: null,
        },
      });
    assert.equal(replyOf(events), "Sunny and 22.2C in Paris.");
  });

  it("parks a callback tool's call until the client answers, under one bracket", async () => {
    const handle = await startAgent("StrandsAskUserAgent");
    await send(handle, "ask", { text: "Pick a city with me." });
    const events = await collect(client, handle.workflowId, {
      onEvent: async (e) => {
        if (e.event.type === "callback_requested")
          await new AgentClient(client, handle.workflowId).provideCallbackResult(e.event.tool_id, {
            result: "Lisbon",
          });
      },
    });

    assert.deepStrictEqual(typesFor(events, "tu-ask"), [
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
    // The model answered with the tool result it was sent: the client's answer.
    assert.equal(replyOf(events), "Lisbon");
  });

  it("sends a message that joins the turn to the model with the next tool result", async () => {
    const handle = await startAgent("StrandsSteeredAgent");
    const asked = await send(handle, "ask", { text: "Pick a city for my trip." });
    let steerer = "";
    const events = await collect(client, handle.workflowId, {
      onEvent: async (e) => {
        if (e.event.type === "callback_requested")
          steerer = (await send(handle, "ask", { text: "Only cities by the sea." })).message_id;
        if (e.event.type === "steering_enqueued")
          await new AgentClient(client, handle.workflowId).provideCallbackResult("tu-ask", {
            result: "Lisbon",
          });
      },
    });

    const answer = events.find(
      (e) => e.event.type === "message_handler_end" && e.message_id === asked.message_id,
    );
    const text = (answer?.event as unknown as { output: { text: string } }).output.text;
    assert.ok(text.startsWith("Lisbon\n\n<steering_messages>"), text);
    assert.ok(text.includes("<message>Only cities by the sea.</message>"), text);
    // tool_end keeps the tool's own output; the steering went only to the model.
    assertMatch(one(events, "tool_end"), { tool_id: "tu-ask", tool_output: "Lisbon" });
    const enqueued = events.find((e) => e.event.type === "steering_enqueued")!;
    assert.equal(enqueued.message_id, steerer);
    assertMatch(one(events, "steering_claimed"), {
      steering_ids: [one(events, "steering_enqueued").steering_id],
      tool_id: "tu-ask",
    });
  });

  async function mcpTurn(approved: boolean) {
    const handle = await startAgent(approved ? "StrandsMCPApprovedAgent" : "StrandsMCPDeniedAgent");
    await send(handle, "ask", { text: "Echo hello." });
    return collect(client, handle.workflowId, {
      onEvent: async (e) => {
        if (e.event.type === "tool_approval_requested")
          await new AgentClient(client, handle.workflowId).approveTool(e.event.tool_id, {
            approved,
            reason: approved ? null : "not allowed",
          });
      },
    });
  }

  it("runs an MCP tool call once a person approves it, under the model's toolUseId", async () => {
    const before = EchoMcpClient.calls.length;
    const events = await mcpTurn(true);

    assert.deepStrictEqual(typesFor(events, "tu-mcp"), [
      "tool_requested",
      "tool_approval_requested",
      "tool_approval_resolved",
      "tool_start",
      "tool_end",
    ]);
    assertMatch(one(events, "tool_end"), { tool_name: "echo", tool_output: "hello" });
    assert.deepStrictEqual(EchoMcpClient.calls.slice(before), [{ message: "hello" }]);
    assert.equal(replyOf(events), "hello");
  });

  it("never sends a denied MCP tool call to the server, and tells the model why", async () => {
    const before = EchoMcpClient.calls.length;
    const events = await mcpTurn(false);

    assert.deepStrictEqual(typesFor(events, "tu-mcp"), [
      "tool_requested",
      "tool_approval_requested",
      "tool_approval_resolved",
    ]);
    assert.equal(EchoMcpClient.calls.length, before);
    assert.match(replyOf(events), /not allowed/);
  });

  it("refuses a Strands interrupt instead of pausing the run", async () => {
    const handle = await startAgent("StrandsInterruptingAgent");
    await send(handle, "ask", { text: "Weather in Paris?" });
    const events = await collect(client, handle.workflowId);

    const failure = one(events, "message_handler_error");
    assert.ok(failure.message.startsWith("Strands interrupts are not supported"), failure.message);
    assert.ok(failure.message.includes("callbackTool"), failure.message);
    assert.equal(events.at(-1)?.event.type, "turn_end");
    // The hook interrupted before the tool ran, so it never started.
    assert.deepStrictEqual(
      byType(events, "tool_requested").map((e) => (e as { tool_name: string }).tool_name),
      ["get_weather"],
    );
    assert.equal(byType(events, "tool_start").length, 0);
  });

  it("runs an OpenAI Agents agent on the same worker as the Strands agents", async () => {
    const handle = await startAgent("OpenAIWeatherAgent");
    await send(handle, "ask", { text: "What's the weather in Paris?" });
    const events = await collect(client, handle.workflowId);

    assertMatch(one(events, "message_handler_end"), { output: { text: OPENAI_WEATHER_REPLY } });
  });
});
