// Worker for the hello-world Strands Agents agent.
//
// Run from the repo root with:
//     bazel run //typescript/examples/strands_hello:worker
//
// Hosts the StrandsHelloAgentTs workflow. Two plugins:
//   * `StrandsPlugin`: the Strands model activity, with the harness observer that streams each
//     model call onto its turn, and the bundler settings Strands' workflow code needs.
//   * `AgentHarnessPlugin`, after it: the harness's data converter and activities, plus the
//     activity body of `get_local_time`, the agent's activity tool.
//
// Env vars (set in .env.local; see .env.example):
//     TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
//     OPENAI_API_KEY                            required: the agent calls the OpenAI API
//     STRANDS_HELLO_TS_TASK_QUEUE               task queue to poll (default: strands-hello-ts)
//     TASK_QUEUE_PREFIX                         put in front of the default queue's name, if set

import { fileURLToPath } from "node:url";
import { OpenAIModel } from "@strands-agents/sdk/models/openai";
import { loadClientConnectConfig } from "@temporalio/envconfig";
import { NativeConnection, Worker } from "@temporalio/worker";
import {
  AgentHarnessPlugin,
  taskQueueName,
  toolActivity,
} from "@temporalio/agent-harness-worker";
import { StrandsPlugin } from "@temporalio/agent-harness-worker/ai-sdks/strands-agents";
import { harnessObserverFactory } from "@temporalio/agent-harness-worker/ai-sdks/strands-agents-harness";
import { MODEL, TASK_QUEUE, getLocalTime } from "./agents.ts";

const OPENAI_MODEL_ID = "gpt-5.1";

// Under `bazel run`, run from the workspace root, so repo-relative paths (.env.local's
// TEMPORAL_CONFIG_FILE) resolve.
if (process.env.BUILD_WORKSPACE_DIRECTORY) process.chdir(process.env.BUILD_WORKSPACE_DIRECTORY);

if (!process.env.OPENAI_API_KEY) {
  console.error("error: OPENAI_API_KEY env var not set");
  process.exit(1);
}

/** The local time in an IANA time zone, e.g. "Saturday 14:05 (CET)". */
function localTime({ time_zone }: { time_zone: string }): Promise<string> {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: time_zone,
    weekday: "long",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZoneName: "short",
  }).formatToParts(new Date());
  const part = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return Promise.resolve(
    `${part("weekday")} ${part("hour")}:${part("minute")} (${part("timeZoneName")})`,
  );
}

const { connectionOptions, namespace } = loadClientConnectConfig();
const connection = await NativeConnection.connect(connectionOptions);
const worker = await Worker.create({
  connection,
  namespace: namespace ?? "default",
  taskQueue: process.env.STRANDS_HELLO_TS_TASK_QUEUE || taskQueueName(TASK_QUEUE),
  workflowsPath: fileURLToPath(new URL("./agents.js", import.meta.url)),
  // The harness's plugin goes last, as in Python.
  plugins: [
    new StrandsPlugin({
      // Built lazily in the model activity, which reads OPENAI_API_KEY from the environment.
      models: { [MODEL]: () => new OpenAIModel({ modelId: OPENAI_MODEL_ID }) },
      observerFactory: harnessObserverFactory,
    }),
    new AgentHarnessPlugin({ tools: [toolActivity(getLocalTime, localTime)] }),
  ],
});
await worker.run();
await connection.close();
