// Runs a worker hosting the scripted agent until SIGINT/SIGTERM, so a driver in another language
// (python/tests/conformance/driver.py) can play the conformance scenarios against it.
//
//   worker --address HOST:PORT --namespace NS --task-queue QUEUE
//
// Each flag falls back to TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE and TEMPORAL_TASK_QUEUE.

import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { runAgentWorker, toolActivities, toolActivity } from "../../src/index.ts";
import { add } from "./scripted-agent.ts";

const { values } = parseArgs({
  options: {
    address: { type: "string" },
    namespace: { type: "string" },
    "task-queue": { type: "string" },
  },
});
// runAgentWorker connects with the same TEMPORAL_* env as the CLI.
if (values.address) process.env.TEMPORAL_ADDRESS = values.address;
if (values.namespace) process.env.TEMPORAL_NAMESPACE = values.namespace;
const taskQueue = values["task-queue"] ?? process.env.TEMPORAL_TASK_QUEUE;
if (!taskQueue) {
  console.error("error: pass --task-queue or set TEMPORAL_TASK_QUEUE");
  process.exit(2);
}

const stop = new AbortController();
for (const signal of ["SIGINT", "SIGTERM"] as const) process.once(signal, () => stop.abort());

await runAgentWorker({
  agents: fileURLToPath(new URL("./scripted-agent.js", import.meta.url)),
  taskQueue,
  activities: toolActivities(toolActivity(add, async ({ a, b }) => a + b)),
  name: "conformance",
  signal: stop.signal,
});
