// Worker for the hello-world OpenAI Agents agent.
//
// Run from the repo root with:
//     bazel run //typescript/examples/openai_hello:worker
//
// Hosts only the OpenAIHelloAgentTs workflow. Its one tool (`get_weather`) is an inline workflow
// tool with no worker-side body, so there are no tool activities to register: the OpenAI Agents
// plugin registers the model activities (including the streaming one) itself, and
// `AgentHarnessPlugin` adds the harness's data converter and activities.
//
// The plugin's default observer factory is the harness one, which translates each streamed model
// call's raw OpenAI events into the harness turn-stream vocabulary live; `runStreamed` in
// agents.ts routes every model call to its turn.
//
// Env vars (set in .env.local; see .env.example):
//     TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
//     OPENAI_API_KEY                            required: the agent calls the OpenAI API
//     OPENAI_HELLO_TS_TASK_QUEUE                task queue to poll (default: openai-hello-ts)

import { fileURLToPath } from "node:url";
import { loadClientConnectConfig } from "@temporalio/envconfig";
import { NativeConnection, Worker } from "@temporalio/worker";
import { AgentHarnessPlugin } from "@temporalio/agent-harness-worker";
import { OpenAIAgentsPlugin } from "@temporalio/agent-harness-worker/ai-sdks/openai-agents";
import { TASK_QUEUE } from "./agents.ts";

// Under `bazel run`, run from the workspace root, so repo-relative paths (.env.local's
// TEMPORAL_CONFIG_FILE) resolve.
if (process.env.BUILD_WORKSPACE_DIRECTORY) process.chdir(process.env.BUILD_WORKSPACE_DIRECTORY);

if (!process.env.OPENAI_API_KEY) {
  console.error("error: OPENAI_API_KEY env var not set");
  process.exit(1);
}

const { connectionOptions, namespace } = loadClientConnectConfig();
const connection = await NativeConnection.connect(connectionOptions);
const worker = await Worker.create({
  connection,
  namespace: namespace ?? "default",
  taskQueue: process.env.OPENAI_HELLO_TS_TASK_QUEUE || TASK_QUEUE,
  workflowsPath: fileURLToPath(new URL("./agents.js", import.meta.url)),
  // Model activity timeouts are the workflow's own (agents.ts): a plugin's `modelParams` reach
  // only the workflows a client carrying the plugin starts, and the session manager starts these.
  // The harness's plugin goes last, as in Python.
  plugins: [new OpenAIAgentsPlugin(), new AgentHarnessPlugin()],
});
await worker.run();
await connection.close();
