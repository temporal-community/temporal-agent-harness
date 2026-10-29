// Runs a worker hosting the scripted agent until SIGINT/SIGTERM, so a driver in another language
// (python/tests/conformance/driver.py) can play the conformance scenarios against it.
//
//   worker --address HOST:PORT --namespace NS --task-queue QUEUE
//
// Each flag falls back to TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE and TEMPORAL_TASK_QUEUE.

import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { NativeConnection, Worker } from "@temporalio/worker";
import { AgentHarnessPlugin, toolActivity } from "../../src/index.ts";
import { add } from "./scripted-agent.ts";

const { values } = parseArgs({
  options: {
    address: { type: "string" },
    namespace: { type: "string" },
    "task-queue": { type: "string" },
  },
});
const taskQueue = values["task-queue"] ?? process.env.TEMPORAL_TASK_QUEUE;
if (!taskQueue) {
  console.error("error: pass --task-queue or set TEMPORAL_TASK_QUEUE");
  process.exit(2);
}

const connection = await NativeConnection.connect({
  address: values.address ?? process.env.TEMPORAL_ADDRESS ?? "localhost:7233",
});
const worker = await Worker.create({
  connection,
  namespace: values.namespace ?? process.env.TEMPORAL_NAMESPACE ?? "default",
  taskQueue,
  workflowsPath: fileURLToPath(new URL("./scripted-agent.js", import.meta.url)),
  plugins: [new AgentHarnessPlugin({ tools: [toolActivity(add, async ({ a, b }) => a + b)] })],
});
console.log(`conformance worker ready: taskQueue=${taskQueue}`);
await worker.run();
await connection.close();
