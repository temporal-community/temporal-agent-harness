// MontyChatAgentTs on a dev server against a scripted Gemini client, so no network: the model
// runs a Code Mode script that calls host functions, reads the script's result, and streams its
// reply.

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { after, before, describe, it } from "node:test";
import type { Interactions } from "@google/genai";
import { Client, Connection } from "@temporalio/client";
import { TestWorkflowEnvironment } from "@temporalio/testing";
import { NativeConnection, Worker } from "@temporalio/worker";
import type { AgentEvent } from "@temporalio/agent-harness/harness/agent-protocol";
import { AgentHarnessPlugin } from "@temporalio/agent-harness-worker";
import { AgentClient } from "@temporalio/agent-harness-worker/harness/agent-client";
import {
  GoogleGenAIPlugin,
  type GeminiInteractionsClient,
} from "@temporalio/agent-harness-worker/ai-sdks/google-genai-plugin";
import { TOOL_ACTIVITIES } from "./activity-bodies.ts";

const AGENTS = fileURLToPath(new URL("./agents.js", import.meta.url));

type Event = Interactions.InteractionSSEEvent;

const SCRIPT = `import asyncio
async def main():
    trip = await open_trip({"description": "SFO to JFK, Jul 1", "traveler": "Ada Lovelace"})
    flights = await search_flights({"origin": "SFO", "destination": "JFK", "date": "2026-07-01"})
    return [trip["trip_id"], flights["flights"][0]["flight_id"]]
asyncio.run(main())`;

/** One model call: an optional function call, then the reply text in chunks. */
function interaction(id: string, call: { id: string; args: object } | null, ...text: string[]) {
  const events: Event[] = [
    { event_type: "interaction.created", interaction: { id, status: "in_progress" } },
  ];
  if (call !== null) {
    const step = {
      type: "function_call",
      id: call.id,
      name: "run_travel_code",
      arguments: {},
    } as const;
    events.push(
      { event_type: "step.start", index: 0, step },
      {
        event_type: "step.delta",
        index: 0,
        delta: { type: "arguments_delta", arguments: JSON.stringify(call.args) },
      },
      { event_type: "step.stop", index: 0 },
    );
  } else {
    events.push(
      { event_type: "step.start", index: 0, step: { type: "model_output" } },
      ...text.map((t): Event => ({
        event_type: "step.delta",
        index: 0,
        delta: { type: "text", text: t },
      })),
      { event_type: "step.stop", index: 0 },
    );
  }
  events.push({ event_type: "interaction.completed", interaction: { id, status: "completed" } });
  return events;
}

/** A Gemini client whose n-th `interactions.create` streams `scripts[n]`, recording requests. */
class ScriptedGemini implements GeminiInteractionsClient {
  readonly requests: Parameters<GeminiInteractionsClient["interactions"]["create"]>[0][] = [];
  readonly interactions: GeminiInteractionsClient["interactions"];

  constructor(scripts: readonly Event[][]) {
    this.interactions = {
      create: async (params) => {
        const script = scripts[this.requests.length];
        this.requests.push(structuredClone(params));
        if (script === undefined) throw new Error(`no script for call ${this.requests.length}`);
        return (async function* () {
          yield* script;
        })();
      },
    };
  }
}

describe("MontyChatAgentTs", () => {
  let env: TestWorkflowEnvironment;
  let nativeConnection: NativeConnection;
  let connection: Connection;
  let client: Client;
  let worker: Worker;
  let running: Promise<void>;
  const taskQueue = `monty-chat-${randomUUID()}`;
  const gemini = new ScriptedGemini([
    interaction("int-1", { id: "call_1", args: { script: SCRIPT } }),
    interaction("int-2", null, "Your trip is open, ", "and FL-SFOJFK-001 is on offer."),
  ]);

  before(async () => {
    const cli = process.env.TEMPORAL_CLI_PATH;
    if (!cli) throw new Error("TEMPORAL_CLI_PATH is not set; run the test with Bazel");
    env = await TestWorkflowEnvironment.createLocal({
      server: { executable: { type: "existing-path", path: resolve(cli) } },
    });
    nativeConnection = await NativeConnection.connect({ address: env.address });
    worker = await Worker.create({
      connection: nativeConnection,
      taskQueue,
      workflowsPath: AGENTS,
      plugins: [
        new GoogleGenAIPlugin(gemini),
        new AgentHarnessPlugin({ tools: TOOL_ACTIVITIES }),
      ],
    });
    running = worker.run();
    connection = await Connection.connect({ address: env.address });
    client = new Client({ connection });
  });
  after(async () => {
    worker.shutdown();
    await running;
    await nativeConnection.close();
    await connection.close();
    await env.teardown();
  });

  it("runs the model's Code Mode script over host functions, then streams the reply", async () => {
    const workflowId = `monty-chat-${randomUUID()}`;
    const handle = await client.workflow.start("MontyChatAgentTs", {
      taskQueue,
      workflowId,
      args: [{}],
    });
    try {
      const agent = new AgentClient(client, workflowId);
      const events: AgentEvent["event"][] = [];
      for await (const [item] of await agent.sendMessage("ask", { text: "SFO to JFK on Jul 1" })) {
        if (item instanceof Error) throw item;
        events.push(item.event);
        // The script and its travel host calls are gated; the board's are allowed by name.
        if (item.event.type === "tool_approval_requested")
          await agent.approveTool(item.event.tool_id, { approved: true });
      }

      const gated = events.flatMap((e) =>
        e.type === "tool_approval_requested" ? [e.tool_name] : [],
      );
      assert.deepEqual(gated, ["run_travel_code", "search_flights"]);
      const ended = events.flatMap((e) => (e.type === "tool_end" ? [e.tool_name] : []));
      assert.deepEqual(ended, ["open_trip", "search_flights", "run_travel_code"]);
      assert.ok(
        events.some((e) => e.type === "state_patch" && e.state_id === "trip_board"),
        "opening the trip patches the board",
      );

      // The script's result went back to the model as the function result.
      const [, second] = gemini.requests;
      const result = second!.input.at(-1) as Interactions.FunctionResultStep;
      assert.equal(result.type, "function_result");
      assert.equal(result.call_id, "call_1");
      assert.match(String(result.result), /^result: \['TRIP-[0-9A-F]{6}', 'FL-SFOJFK-001'\]$/);

      const deltas = events.flatMap((e) => (e.type === "reply_delta" ? [e.text] : []));
      assert.deepEqual(deltas, ["Your trip is open, ", "and FL-SFOJFK-001 is on offer."]);
      assert.deepEqual(
        events.find((e) => e.type === "message_handler_end"),
        {
          type: "message_handler_end",
          output: { text: "Your trip is open, and FL-SFOJFK-001 is on offer." },
        },
      );
      assert.equal(events.at(-1)!.type, "turn_end");
    } finally {
      await handle.terminate("test finished").catch(() => undefined);
    }
  });
});
