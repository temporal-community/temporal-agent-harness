// StrandsHelloAgentTs end to end on a dev server, its model calls answered by a scripted Strands
// model registered under the example's model name (no network): the model asks the user a
// question, a message sent while that callback tool waits steers the turn, and the steering
// reaches the model with the tool's result.

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { after, before, it } from "node:test";
import { fileURLToPath } from "node:url";
import { Model } from "@strands-agents/sdk";
import type { BaseModelConfig, Message, ModelStreamEvent } from "@strands-agents/sdk";
import type { AgentEvent } from "@temporalio/agent-harness/harness/agent-protocol";
import { AgentHarnessPlugin, toolActivity } from "@temporalio/agent-harness-worker";
import { StrandsPlugin } from "@temporalio/agent-harness-worker/ai-sdks/strands-agents";
import { harnessObserverFactory } from "@temporalio/agent-harness-worker/ai-sdks/strands-agents-harness";
import { AgentClient } from "@temporalio/agent-harness-worker/harness/agent-client";
import { Client } from "@temporalio/client";
import { TestWorkflowEnvironment } from "@temporalio/testing";
import { NativeConnection, Worker } from "@temporalio/worker";
import { MODEL, getLocalTime } from "./agents.ts";

const AGENTS = fileURLToPath(new URL("./agents.js", import.meta.url));
const ASK_ID = "tu-ask";

/** Calls ask_user, then answers with exactly the tool result it was sent. */
class AskThenEchoModel extends Model<BaseModelConfig> {
  #calls = 0;

  override updateConfig(_config: BaseModelConfig): void {}

  override getConfig(): BaseModelConfig {
    return { modelId: "scripted" };
  }

  override async *stream(messages: Message[]): AsyncIterable<ModelStreamEvent> {
    this.#calls += 1;
    yield { type: "modelMessageStartEvent", role: "assistant" };
    if (this.#calls === 1) {
      yield {
        type: "modelContentBlockStartEvent",
        start: { type: "toolUseStart", name: "ask_user", toolUseId: ASK_ID },
      };
      yield {
        type: "modelContentBlockDeltaEvent",
        delta: { type: "toolUseInputDelta", input: '{"question": "Which city?"}' },
      };
      yield { type: "modelContentBlockStopEvent" };
      yield { type: "modelMessageStopEvent", stopReason: "toolUse" };
      return;
    }
    let text = "";
    for (const block of messages.at(-1)?.content ?? [])
      if (block.type === "toolResultBlock")
        for (const c of block.content) if (c.type === "textBlock") text += c.text;
    yield { type: "modelContentBlockStartEvent" };
    yield { type: "modelContentBlockDeltaEvent", delta: { type: "textDelta", text } };
    yield { type: "modelContentBlockStopEvent" };
    yield { type: "modelMessageStopEvent", stopReason: "endTurn" };
  }
}

let env: TestWorkflowEnvironment;
let connection: NativeConnection;
let worker: Worker;
let running: Promise<void>;
const taskQueue = `strands-hello-${randomUUID()}`;

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
      new StrandsPlugin({
        models: { [MODEL]: () => new AskThenEchoModel() },
        observerFactory: harnessObserverFactory,
      }),
      new AgentHarnessPlugin({
        largePayloadOffload: null,
        tools: [toolActivity(getLocalTime, async () => "Saturday 14:05 (CET)")],
      }),
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

it("steers the turn with a message sent while ask_user waits for the user", async () => {
  const client: Client = env.client;
  const workflowId = `strands-hello-${randomUUID()}`;
  await client.workflow.start("StrandsHelloAgentTs", { taskQueue, workflowId, args: [{}] });
  const agentClient = new AgentClient(client, workflowId);

  const asked = await agentClient.submitMessage("ask", { text: "Plan a trip for me." });
  const events: AgentEvent[] = [];
  let steerer = "";
  for await (const [event] of await agentClient.attach({ fromOffset: 0 })) {
    events.push(event);
    if (event.event.type === "callback_requested") {
      // The turn is open, waiting on ask_user: this message steers it.
      steerer = (await agentClient.submitMessage("ask", { text: "Only cities by the sea." }))
        .message_id;
      await agentClient.provideCallbackResult(ASK_ID, { result: "Lisbon" });
    }
    if (event.event.type === "turn_end") break;
  }
  await client.workflow.getHandle(workflowId).terminate("test finished");

  const outputOf = (messageId: string) =>
    (
      events.find(
        (e) => e.event.type === "message_handler_end" && e.message_id === messageId,
      )?.event as unknown as { output: { text: string } }
    ).output.text;
  assert.equal(outputOf(steerer), "");
  const [toolResult, steering] = outputOf(asked.message_id).split("\n\n");
  assert.equal(toolResult, "Lisbon");
  assert.ok(steering?.includes("<message>Only cities by the sea.</message>"), steering);
  const claimed = events.find((e) => e.event.type === "steering_claimed");
  assert.equal((claimed?.event as { tool_id?: string } | undefined)?.tool_id, ASK_ID);
});
