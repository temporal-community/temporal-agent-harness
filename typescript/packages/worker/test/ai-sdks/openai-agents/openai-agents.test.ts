// The OpenAI Agents SDK integration end to end: the harness plugin's streamed model activity takes
// the official one's place, and a turn running an Agents SDK agent on a dev server (its model
// calls answered by a scripted provider) publishes the harness event sequence.

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { after, before, describe, it } from "node:test";
import type { CallToolResultContent, MCPServer, StreamEvent } from "@openai/agents-core";
import { Context } from "@temporalio/activity";
import { Client, Connection } from "@temporalio/client";
import { ApplicationFailure, type Duration } from "@temporalio/common";
import {
  StatelessMCPServerProvider,
  OpenAIAgentsPlugin as TemporalOpenAIAgentsPlugin,
} from "@temporalio/openai-agents";
import { MockActivityEnvironment } from "@temporalio/testing";
import { NativeConnection, Worker, type WorkerOptions } from "@temporalio/worker";
import type { AgentEvent } from "@temporalio/agent-harness/harness/agent-protocol";
import { AgentClient } from "../../../src/harness/agent-client.ts";
import {
  INVOKE_MODEL_STREAM_ACTIVITY,
  OpenAIAgentsPlugin,
  type InvokeModelStreamActivityInput,
} from "../../../src/ai-sdks/openai-agents/index.ts";
import type { ObserverFactory } from "../../../src/ai-sdks/integration-helpers/stream-observer.ts";
import {
  harnessObserverFactory,
  type HarnessStreamToken,
} from "../../../src/ai-sdks/openai-agents-harness.ts";
import { AgentHarnessPlugin, toolActivity } from "../../../src/index.ts";
import { assertMatch } from "../../match.ts";
import { collect, one, send, startServer, typesFor, type TemporalServer } from "../../support.ts";
import {
  ASK_CALL_ID,
  ASK_REPLY,
  MCP_CALL_ID,
  MCP_ERROR_CALL_ID,
  MCP_ERROR_REPLY,
  MCP_REPLY,
  PROBE_MCP_SERVER,
  SCRIPTED_ASK_MODEL,
  SCRIPTED_MCP_ERROR_MODEL,
  SCRIPTED_MCP_MODEL,
  SCRIPTED_MODEL,
  UNKNOWN_TERM,
  WEATHER_CALL_ID,
  WEATHER_REPLY,
  askScript,
  mcpErrorScript,
  mcpScript,
  scriptedProvider,
  weatherScript,
} from "./scripted-model.ts";
import { getWeatherActivity } from "./tools.ts";

const WORKFLOWS = fileURLToPath(new URL("./workflows.js", import.meta.url));

/** An in-process MCP server with one tool, `lookup`. */
const lookupServer = (): MCPServer => ({
  name: PROBE_MCP_SERVER,
  cacheToolsList: false,
  async connect() {},
  async close() {},
  async listTools() {
    return [
      {
        name: "lookup",
        description: "Look a term up.",
        inputSchema: {
          type: "object",
          properties: { q: { type: "string" } },
          required: ["q"],
          additionalProperties: false,
        },
      },
    ];
  },
  async callTool(_name, args) {
    if (args?.q !== UNKNOWN_TERM) return [{ type: "text", text: `found ${String(args?.q)}` }];
    // As agents-core's own servers report a failed call: a non-enumerable flag on the content.
    const content: CallToolResultContent = [
      { type: "text", text: `no such term: ${UNKNOWN_TERM}` },
    ];
    Object.defineProperty(content, "isError", { value: true, enumerable: false });
    return content;
  },
  async invalidateToolsCache() {},
});

type Activities = Record<string, (...args: never[]) => Promise<unknown>>;

/** The activities a worker ends up with after `plugins` configure it, in order. */
const registeredBy = (...plugins: { configureWorker(o: WorkerOptions): WorkerOptions }[]) =>
  plugins.reduce((o, p) => p.configureWorker(o), { taskQueue: "plugin-test" } as WorkerOptions)
    .activities as Activities;

const badRouteInput: InvokeModelStreamActivityInput = {
  modelName: SCRIPTED_MODEL,
  request: {
    input: "hi",
    modelSettings: {},
    tools: [],
    outputType: "text",
    handoffs: [],
    tracing: false,
  },
  streamingTopic: "a-raw-topic",
};

describe("OpenAIAgentsPlugin", () => {
  const modelProvider = scriptedProvider({});

  it("registers the harness's invokeModelStreamActivity in place of the official one", async () => {
    const ours = registeredBy(new OpenAIAgentsPlugin({ modelProvider }));
    const official = registeredBy(new TemporalOpenAIAgentsPlugin({ modelProvider }));
    assert.deepStrictEqual(Object.keys(ours).sort(), Object.keys(official).sort());
    assert.notEqual(ours[INVOKE_MODEL_STREAM_ACTIVITY], official[INVOKE_MODEL_STREAM_ACTIVITY]);
    // Only the harness's activity reads streamingTopic as a turn route, and refuses a raw topic.
    await assert.rejects(
      new MockActivityEnvironment().run(
        ours[INVOKE_MODEL_STREAM_ACTIVITY] as (
          i: InvokeModelStreamActivityInput,
        ) => Promise<unknown>,
        badRouteInput,
      ),
      (e: unknown) => e instanceof ApplicationFailure && e.type === "HarnessStreamRouteMissing",
    );
  });

  it("keeps the harness's streamed activity beside AgentHarnessPlugin, in either order", () => {
    const plugin = new OpenAIAgentsPlugin({ modelProvider });
    const ours = registeredBy(plugin)[INVOKE_MODEL_STREAM_ACTIVITY];
    const harness = new AgentHarnessPlugin({ largePayloadOffload: null });
    assert.equal(registeredBy(plugin, harness)[INVOKE_MODEL_STREAM_ACTIVITY], ours);
    assert.equal(registeredBy(harness, plugin)[INVOKE_MODEL_STREAM_ACTIVITY], ours);
  });

  it("wins over a worker's own activity of the same name, as the official plugin does", () => {
    const plugin = new OpenAIAgentsPlugin({ modelProvider });
    const mine = async () => [];
    const activities = plugin.configureWorker({
      taskQueue: "plugin-test",
      activities: { [INVOKE_MODEL_STREAM_ACTIVITY]: mine },
    } as WorkerOptions).activities as Activities;
    assert.notEqual(activities[INVOKE_MODEL_STREAM_ACTIVITY], mine);
  });

  it("refuses hostedToolCredentials, which its streamed activity does not inject", () => {
    assert.throws(
      () => new OpenAIAgentsPlugin({ modelProvider, hostedToolCredentials: () => ({}) }),
      /hostedToolCredentials is not supported/,
    );
  });

  it("refuses a plugin-level streamingTopic, which would override each run's turn route", () => {
    assert.throws(
      () => new OpenAIAgentsPlugin({ modelProvider, modelParams: { streamingTopic: "raw" } }),
      /streamingTopic must be unset/,
    );
  });
});

describe("an Agents SDK turn on a dev server", () => {
  let server: TemporalServer;
  let nativeConnection: NativeConnection;
  let connection: Connection;
  let client: Client;
  let worker: Worker;
  let running: Promise<void>;
  const taskQueue = `test-openai-agents-${randomUUID()}`;
  const started: string[] = [];
  const modelProvider = scriptedProvider({
    [SCRIPTED_MODEL]: weatherScript,
    [SCRIPTED_ASK_MODEL]: askScript,
    [SCRIPTED_MCP_MODEL]: mcpScript,
    [SCRIPTED_MCP_ERROR_MODEL]: mcpErrorScript,
  });
  const observed: {
    token: HarnessStreamToken;
    batchInterval: Duration | undefined;
    startToCloseTimeoutMs: number | undefined;
  }[] = [];
  // Runs in the model activity, so it reads the options the activity was scheduled with.
  const spyingFactory: ObserverFactory<HarnessStreamToken, StreamEvent> = (token, options) => {
    observed.push({
      token,
      batchInterval: options.batchInterval,
      startToCloseTimeoutMs: Context.current().info.startToCloseTimeoutMs,
    });
    return harnessObserverFactory(token, options);
  };

  before(async () => {
    server = await startServer();
    nativeConnection = await NativeConnection.connect({ address: server.address });
    worker = await Worker.create({
      connection: nativeConnection,
      namespace: server.namespace,
      taskQueue,
      workflowsPath: WORKFLOWS,
      plugins: [
        new OpenAIAgentsPlugin({
          modelProvider,
          observerFactory: spyingFactory,
          mcpServerProviders: [new StatelessMCPServerProvider(PROBE_MCP_SERVER, lookupServer)],
        }),
        new AgentHarnessPlugin({
          largePayloadOffload: null,
          tools: [toolActivity(getWeatherActivity, async ({ city }) => `activity: sunny in ${city}`)],
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

  async function startAgent(workflowType: string, by: Client = client) {
    const workflowId = `${workflowType}-${randomUUID()}`;
    const handle = await by.workflow.start(workflowType, { taskQueue, workflowId, args: [{}] });
    started.push(workflowId);
    return handle;
  }

  /** The start-to-close timeout of each model activity since `observed` was last cleared. */
  const modelTimeouts = () => observed.map((o) => o.startToCloseTimeoutMs);

  const types = (events: AgentEvent[]) => events.map((e) => e.event.type);

  const MODEL_CALL_WITH_TOOL = [
    "model_interaction_started",
    "tool_requested",
    "model_interaction_ended",
  ];
  const TOOL_RUN = ["tool_start", "tool_end"];
  const MODEL_CALL_WITH_REPLY = [
    "model_interaction_started",
    "reply_delta",
    "reply_delta",
    "model_interaction_ended",
  ];
  const TURN_OPEN = ["message_accepted", "turn_started", "message_handler_start"];
  const TURN_CLOSE = ["message_handler_end", "turn_end"];

  it("streams both model calls and runs the tool under the model's call id", async () => {
    observed.length = 0;
    const handle = await startAgent("OpenAIWeatherAgent");
    await send(handle, "ask", { text: "What's the weather in Paris?" });
    const events = await collect(client, handle.workflowId);

    assert.deepStrictEqual(types(events), [
      ...TURN_OPEN,
      ...MODEL_CALL_WITH_TOOL,
      ...TOOL_RUN,
      ...MODEL_CALL_WITH_REPLY,
      ...TURN_CLOSE,
    ]);
    const byType = (type: string) =>
      events.filter((e) => e.event.type === type).map((e) => e.event);
    assert.deepStrictEqual(byType("tool_requested"), [
      {
        type: "tool_requested",
        tool_id: WEATHER_CALL_ID,
        tool_name: "get_weather",
        tool_input: { city: "Paris" },
      },
    ]);
    assert.deepStrictEqual(byType("tool_end"), [
      {
        type: "tool_end",
        tool_id: WEATHER_CALL_ID,
        tool_name: "get_weather",
        tool_output: "It's 72F and sunny in Paris.",
      },
    ]);
    assert.deepStrictEqual(
      byType("reply_delta").map((e) => (e as { text: string }).text).join(""),
      WEATHER_REPLY,
    );
    for (const ended of byType("model_interaction_ended"))
      assert.deepStrictEqual(ended, {
        type: "model_interaction_ended",
        model: SCRIPTED_MODEL,
        usage: {
          input_tokens: 11,
          output_tokens: 22,
          total_tokens: 33,
          cached_tokens: 5,
          thought_tokens: 7,
          tool_use_tokens: null,
        },
      });
    assert.deepStrictEqual(byType("message_handler_end"), [
      { type: "message_handler_end", output: { text: WEATHER_REPLY } },
    ]);

    // The harness tool reached the model in OpenAI's strict mode.
    const { tools } = modelProvider.requests.at(-1)!;
    assert.deepStrictEqual(tools, [
      {
        type: "function",
        name: "get_weather",
        description: "Return the current weather for a city.",
        parameters: {
          type: "object",
          properties: { city: { type: "string" } },
          required: ["city"],
          additionalProperties: false,
        },
        strict: true,
      },
    ]);

    // Both model calls ran the harness's activity: its observer factory saw each, routed to
    // this turn at Python's default batch interval, scheduled with the workflow's own
    // defaultModelParams.
    const turnId = events[0]!.turn_id;
    assert.deepStrictEqual(
      observed.map(({ token, batchInterval, startToCloseTimeoutMs }) => ({
        turn: token.context.turn_id,
        agent: token.context.agent_id,
        model: token.model,
        batchInterval,
        startToCloseTimeoutMs,
      })),
      Array.from({ length: 2 }, () => ({
        turn: turnId,
        agent: events[0]!.agent_id,
        model: SCRIPTED_MODEL,
        batchInterval: "100 milliseconds",
        startToCloseTimeoutMs: 37_000,
      })),
    );
  });

  it("runs an activity tool's body on the worker under the model's call id", async () => {
    const handle = await startAgent("OpenAIActivityWeatherAgent");
    await send(handle, "ask", { text: "What's the weather in Paris?" });
    const events = await collect(client, handle.workflowId);

    assert.deepStrictEqual(types(events), [
      ...TURN_OPEN,
      ...MODEL_CALL_WITH_TOOL,
      ...TOOL_RUN,
      ...MODEL_CALL_WITH_REPLY,
      ...TURN_CLOSE,
    ]);
    assertMatch(one(events, "tool_end"), {
      tool_id: WEATHER_CALL_ID,
      tool_output: "activity: sunny in Paris",
    });
    // The body's result went back to the model.
    assert.ok(
      JSON.stringify(modelProvider.requests.at(-1)!.input).includes("activity: sunny in Paris"),
    );
  });

  it("parks a callback tool's call until the client answers, then hands the answer to the model", async () => {
    const handle = await startAgent("OpenAIAskUserAgent");
    await send(handle, "ask", { text: "Plan my trip." });
    const events = await collect(client, handle.workflowId, {
      onEvent: async (e) => {
        if (e.event.type === "callback_requested")
          await new AgentClient(client, handle.workflowId).provideCallbackResult(e.event.tool_id, {
            result: "Lisbon",
          });
      },
    });

    assert.deepStrictEqual(typesFor(events, ASK_CALL_ID), [
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
    assert.equal(one(events, "message_handler_end").output.text, ASK_REPLY);
    assert.ok(JSON.stringify(modelProvider.requests.at(-1)!.input).includes("Lisbon"));
  });

  it("lets a starting client's plugin modelParams win over the workflow's own", async () => {
    observed.length = 0;
    const pluginClient = new Client({
      connection,
      namespace: server.namespace,
      plugins: [
        new OpenAIAgentsPlugin({
          modelProvider,
          modelParams: { startToCloseTimeout: "41 seconds" },
        }),
      ],
    });
    const handle = await startAgent("OpenAIWeatherAgent", pluginClient);
    await send(handle, "ask", { text: "What's the weather in Paris?" });
    const events = await collect(client, handle.workflowId);

    assert.equal(events.at(-2)?.event.type, "message_handler_end");
    assert.deepStrictEqual(modelTimeouts(), [41_000, 41_000]);
  });

  it(
    "holds a gated tool for approval between the first model call and the tool's run",
    async () => {
      const handle = await startAgent("OpenAIGatedWeatherAgent");
      const agent = new AgentClient(client, handle.workflowId);
      await send(handle, "ask", { text: "What's the weather in Paris?" });
      const events = await collect(client, handle.workflowId, {
        onEvent: (e) =>
          e.event.type === "tool_approval_requested"
            ? agent.approveTool(e.event.tool_id, { approved: true })
            : undefined,
      });

      assert.deepStrictEqual(types(events), [
        ...TURN_OPEN,
        ...MODEL_CALL_WITH_TOOL,
        "tool_approval_requested",
        "tool_approval_resolved",
        ...TOOL_RUN,
        ...MODEL_CALL_WITH_REPLY,
        ...TURN_CLOSE,
      ]);
      const toolIds = events.flatMap((e) =>
        "tool_id" in e.event ? [(e.event as { tool_id: string }).tool_id] : [],
      );
      assert.deepStrictEqual(new Set(toolIds), new Set([WEATHER_CALL_ID]));
    },
  );

  it("gates a stateless MCP server's call and brackets it under the model's call id", async () => {
    const handle = await startAgent("OpenAIMcpAgent");
    const agent = new AgentClient(client, handle.workflowId);
    await send(handle, "ask", { text: "Look up cats." });
    const events = await collect(client, handle.workflowId, {
      onEvent: (e) =>
        e.event.type === "tool_approval_requested"
          ? agent.approveTool(e.event.tool_id, { approved: true })
          : undefined,
    });

    assert.deepStrictEqual(types(events), [
      ...TURN_OPEN,
      ...MODEL_CALL_WITH_TOOL,
      "tool_approval_requested",
      "tool_approval_resolved",
      ...TOOL_RUN,
      ...MODEL_CALL_WITH_REPLY,
      ...TURN_CLOSE,
    ]);
    const tool = events.flatMap((e) =>
      e.event.type === "tool_start" || e.event.type === "tool_end" ? [e.event] : [],
    );
    assert.deepStrictEqual(tool, [
      { type: "tool_start", tool_id: MCP_CALL_ID, tool_name: "lookup", tool_input: { q: "cats" } },
      { type: "tool_end", tool_id: MCP_CALL_ID, tool_name: "lookup", tool_output: "found cats" },
    ]);
    const toolIds = events.flatMap((e) =>
      "tool_id" in e.event ? [(e.event as { tool_id: string }).tool_id] : [],
    );
    assert.deepStrictEqual(new Set(toolIds), new Set([MCP_CALL_ID]));
    assert.equal(events.at(-2)?.event.type, "message_handler_end");
    assert.deepStrictEqual(events.at(-2)?.event, {
      type: "message_handler_end",
      output: { text: MCP_REPLY },
    });
  });

  it("publishes tool_error for an MCP call whose result reports isError", async () => {
    const handle = await startAgent("OpenAIFailingMcpAgent");
    await send(handle, "ask", { text: `Look up ${UNKNOWN_TERM}.` });
    const events = await collect(client, handle.workflowId);

    assert.deepStrictEqual(types(events), [
      ...TURN_OPEN,
      ...MODEL_CALL_WITH_TOOL,
      "tool_start",
      "tool_error",
      ...MODEL_CALL_WITH_REPLY,
      ...TURN_CLOSE,
    ]);
    assert.deepStrictEqual(
      events.flatMap((e) => (e.event.type === "tool_error" ? [e.event] : [])),
      [
        {
          type: "tool_error",
          tool_id: MCP_ERROR_CALL_ID,
          tool_name: "lookup",
          message: `no such term: ${UNKNOWN_TERM}`,
        },
      ],
    );
    assert.deepStrictEqual(events.at(-2)?.event, {
      type: "message_handler_end",
      output: { text: MCP_ERROR_REPLY },
    });
  });
});
