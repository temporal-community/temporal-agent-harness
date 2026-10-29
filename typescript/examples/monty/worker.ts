// Worker for the TypeScript Monty example agents (Python's worker.py).
//
// Run from typescript/examples/monty with `just worker`, which builds and runs it with Bazel.
//
// Connection settings come from a temporal.toml profile, resolved by @temporalio/envconfig's
// loadClientConnectConfig() from TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE, which the example sets
// in .env.local (see README.md).
//
// Env vars:
//     TEMPORAL_CONFIG_FILE      path to a temporal.toml (set in .env.local)
//     TEMPORAL_PROFILE          profile name to load (default: "default")
//     GEMINI_API_KEY            required: the conversational agents call the Gemini API
//     MONTY_AGENT_TASK_QUEUE   task queue to poll (default: monty-dynamic-agent-ts)
//
// This worker hosts the three Monty agents (MontyDynamicAgentTs + the two conversational agents),
// not the session manager. The packaged session manager is hosted by
// `temporal-agent-harness session-manager`; because it launches agents by registered name, it
// dispatches these agents to this queue without this worker hosting it.

import { fileURLToPath } from "node:url";
import { GoogleGenAI } from "@google/genai";
import { loadClientConnectConfig } from "@temporalio/envconfig";
import { NativeConnection, Worker } from "@temporalio/worker";
import { AgentHarnessPlugin } from "@temporalio/agent-harness-worker";
import { GoogleGenAIPlugin } from "@temporalio/agent-harness-worker/ai-sdks/google-genai-plugin";
import { TOOL_ACTIVITIES } from "./activity-bodies.ts";
import { TASK_QUEUE } from "./workflow.ts";

// Under `bazel run`, run from the workspace root, so repo-relative paths (.env.local's
// TEMPORAL_CONFIG_FILE) resolve.
if (process.env.BUILD_WORKSPACE_DIRECTORY) process.chdir(process.env.BUILD_WORKSPACE_DIRECTORY);

const apiKey = process.env.GEMINI_API_KEY;
if (!apiKey) {
  console.error("error: GEMINI_API_KEY env var not set");
  process.exit(1);
}

const { connectionOptions, namespace } = loadClientConnectConfig();
const connection = await NativeConnection.connect(connectionOptions);
const worker = await Worker.create({
  connection,
  namespace: namespace ?? "default",
  taskQueue: process.env.MONTY_AGENT_TASK_QUEUE ?? TASK_QUEUE,
  // All three Monty agents run here: MontyChatSubagentAgentTs drives MontyDynamicAgentTs as a
  // subagent, so the child runs on this same queue.
  workflowsPath: fileURLToPath(new URL("./agents.js", import.meta.url)),
  // Two plugins, the harness's last, as in Python:
  //   * GoogleGenAIPlugin  — the Gemini interactions activity.
  //   * AgentHarnessPlugin — everything the harness itself needs on this worker: the
  //     large-payload offload converter, the subagent-turn activity (drives the script-runner
  //     child for MontyChatSubagentAgentTs), the durable body of every travel tool, and Code
  //     Mode, from the installed @pydantic/monty, whose sandbox runs in activities here.
  plugins: [
    new GoogleGenAIPlugin(new GoogleGenAI({ apiKey })),
    new AgentHarnessPlugin({ tools: TOOL_ACTIVITIES }),
  ],
});
await worker.run();
await connection.close();
