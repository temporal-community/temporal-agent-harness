// Worker-side harness: publishing to an agent's turn stream from an activity, registering
// activity-tool bodies, and the harness's own activities (the subagent turn, the Jev approver).
// Register them with `new AgentHarnessPlugin()` on the worker, or spread `harnessActivities()`.
// Code Mode's sandbox activities live in `@temporalio/agent-harness-worker/harness/code-mode/monty-stepper`,
// so a worker that does not run Code Mode never loads the Monty binary.

import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Worker, type WorkerOptions } from "@temporalio/worker";
import { connectWorker } from "./web/client.ts";
import { AgentHarnessPlugin } from "./plugin.ts";

export interface RunAgentWorkerOptions {
  /** The module that exports your `defineAgent()` agents, e.g. `new URL("./agents.ts", import.meta.url)`. */
  agents: string | URL;
  taskQueue: string;
  /** Activity-tool bodies (`toolActivities(...)`) and any other activities. */
  activities?: object;
  /** Integration plugins, e.g. `...aiSdk(openai)`. AgentHarnessPlugin is added after them. */
  plugins?: NonNullable<WorkerOptions["plugins"]>;
  bundlerOptions?: WorkerOptions["bundlerOptions"];
  /** Env vars the agents need; the worker exits with a message when one is unset. */
  requiredEnv?: readonly string[];
  /** Name in the ready log line. Default: the task queue. */
  name?: string;
  /** Stops the worker when aborted. Without it the worker runs until the process gets SIGINT/SIGTERM. */
  signal?: AbortSignal;
}

/**
 * Connect and run a worker for `agents` until it is stopped. Connection
 * settings come from the same TEMPORAL_* env and temporal.toml profile as the CLI.
 */
export async function runAgentWorker(options: RunAgentWorkerOptions): Promise<void> {
  const missing = (options.requiredEnv ?? []).filter((name) => !process.env[name]);
  if (missing.length > 0) {
    console.error(`error: ${missing.join(", ")} env var not set`);
    process.exit(1);
  }
  const agents =
    options.agents instanceof URL ? fileURLToPath(options.agents) : resolve(options.agents);
  const { connection, namespace, address } = await connectWorker();
  const worker = await Worker.create({
    connection,
    namespace,
    taskQueue: options.taskQueue,
    workflowsPath: agents,
    ...(options.bundlerOptions ? { bundlerOptions: options.bundlerOptions } : {}),
    ...(options.activities ? { activities: options.activities } : {}),
    plugins: [...(options.plugins ?? []), new AgentHarnessPlugin()],
  });
  console.log(
    `${options.name ?? options.taskQueue} worker ready: address=${address} namespace=${namespace} taskQueue=${options.taskQueue}`,
  );
  const { signal } = options;
  if (signal === undefined) await worker.run();
  else
    await worker.runUntil(
      new Promise<void>((done) =>
        signal.aborted ? done() : signal.addEventListener("abort", () => done(), { once: true }),
      ),
    );
}

export {
  DEFAULT_PUBLISH_BATCH_INTERVAL,
  heartbeatEvery5s,
  heartbeating,
  nonRetryable,
  publisherFromActivity,
  toolActivities,
  toolActivity,
  type ToolActivityContext,
  type TurnEventPublisher,
} from "./harness/agent-workflow.ts";
export { runSubagentTurn } from "./harness/subagent-activities.ts";
export {
  jevToolApproval,
  postSystemOne,
  systemOneEndpoint,
  type SystemOneEndpoint,
  type SystemOneResponse,
} from "./ai-sdks/jev/evaluator.ts";
export {
  AgentHarnessPlugin,
  CODE_MODE_MISSING_EXTRA_ERROR,
  harnessActivities,
  rawTopicAttemptInterceptor,
} from "./plugin.ts";
export {
  DEFAULT_PAYLOAD_STORAGE,
  checkPayloadStorage,
  localFileStorageDriver,
  localPayloadStorage,
  type CheckableStorageDriver,
} from "./utils/large-payload.ts";
export {
  ConfigError,
  collectFilesystemActivities,
  filesystemActivities,
  type FilesystemOps,
  type FilesystemRegistration,
} from "./harness/code-mode/activity-fs.ts";
