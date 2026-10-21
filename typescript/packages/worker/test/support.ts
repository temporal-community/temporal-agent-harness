// Shared plumbing for the workflow-level tests: a Temporal dev server per test file, an in-process
// worker on a unique task queue, the fixture activities, and stream helpers.

import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ApplicationFailure, Context } from "@temporalio/activity";
import { Client, Connection, type WorkflowHandle } from "@temporalio/client";
import { TestWorkflowEnvironment } from "@temporalio/testing";
import {
  NativeConnection,
  Worker,
  bundleWorkflowCode,
  type BundlerPlugin,
} from "@temporalio/worker";
import { WorkflowStreamClient } from "@temporalio/workflow-streams/client";
import type {
  JevApprovalAnswer,
  JevApprovalRequest,
} from "@temporalio/agent-harness/ai-sdks/jev/evaluator";
import {
  AGENT_INTERFACE_QUERY,
  AGENT_STATUS_QUERY,
  SEND_AGENT_MESSAGE_UPDATE,
  TURN_EVENTS_TOPIC,
  type AgentEvent,
  type AgentEventType,
  type AgentMessageReply,
  type StreamEventOf,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { codeModeActivities } from "../src/harness/code-mode/monty-stepper.ts";
import { AgentClient } from "../src/harness/agent-client.ts";
import { subscribe } from "../src/harness/stream-merge/index.ts";
import { DEFAULT_PAYLOAD_STORAGE } from "../src/utils/large-payload.ts";
import {
  harnessActivities,
  rawTopicAttemptInterceptor,
  toolActivities,
  toolActivity,
} from "../src/index.ts";
import {
  type RawModelProbe,
  bigResult,
  bookItem,
  failingTool,
  gatedActivityTool,
  refundCustomer,
  refundWithCredential,
  safeActivityTool,
  searchItems,
  heartbeatTool,
} from "./fixtures/workflows.ts";

const NAMESPACE = "default";
const dataConverter = { externalStorage: DEFAULT_PAYLOAD_STORAGE };
/** The compiled fixture module the worker bundles its workflows from. */
export const FIXTURES = fileURLToPath(new URL("./fixtures/workflows.js", import.meta.url));
export const TEST_CONTINUATION_SECRET = "test-only-continuation-secret-0123456789";

/** What the fake `load_skills` activity seeds VfsProbeAgent's /skills with. */
export const skills: { files: Record<string, string> } = {
  files: { "report/SKILL.md": "Sum the amounts in data.csv.", "report/data.csv": "a,2\nb,3\n" },
};

/** What the fake `jev_tool_approval` answers, and every request it saw. */
export const jev: {
  answer: JevApprovalAnswer | null;
  error: string | null;
  seen: JevApprovalRequest[];
} = { answer: null, error: null, seen: [] };

function fixtureActivities() {
  return {
    ...harnessActivities(),
    ...codeModeActivities({ continuationSecret: TEST_CONTINUATION_SECRET }),
    async load_skills(): Promise<Record<string, string>> {
      return { ...skills.files };
    },
    async jev_tool_approval(request: JevApprovalRequest): Promise<JevApprovalAnswer> {
      jev.seen.push(request);
      if (jev.error !== null)
        throw ApplicationFailure.create({ message: jev.error, nonRetryable: true });
      return jev.answer!;
    },
    // "retry": attempt 1 streams part of a reply and fails, attempt 2 streams a whole one.
    // "abandon": streams part of a reply, then fails for good.
    raw_model_probe: (async (topic, scenario) => {
      const { attempt } = Context.current().info;
      const stream = WorkflowStreamClient.fromWithinActivity();
      const raw = stream.topic<unknown>(topic);
      try {
        raw.publish({ type: "response_started" });
        raw.publish({ type: "output_text_delta", delta: `attempt ${attempt}` });
        if (scenario === "abandon")
          throw ApplicationFailure.create({ message: "model gone", nonRetryable: true });
        if (attempt === 1) throw ApplicationFailure.create({ message: "connection reset" });
        raw.publish({ type: "response_done", response: { usage: {}, output: [] } });
        return `attempt ${attempt}`;
      } finally {
        await stream[Symbol.asyncDispose]();
      }
    }) satisfies RawModelProbe,
    ...toolActivities(
      toolActivity(gatedActivityTool, async ({ text }) => `act:${text}`),
      toolActivity(safeActivityTool, async ({ text }) => `safe:${text}`),
      toolActivity(refundCustomer, async ({ amount }) => `refunded:${amount}`),
      toolActivity(refundWithCredential, async ({ amount }) => `refunded:${amount}`),
      toolActivity(searchItems, async ({ origin, n }) => ({
        items: Array.from({ length: n }, (_, i) => ({ id: `${origin}-${i}`, price: 100 + 10 * i })),
      })),
      toolActivity(bookItem, async ({ id }) => `BOOKED-${id}`),
      toolActivity(bigResult, async ({ n }) => "y".repeat(n)),
      toolActivity(failingTool, async () => {
        throw new Error("tool broke");
      }),
      toolActivity(
        heartbeatTool,
        async ({ text }) =>
          new Promise((resolve) => setTimeout(() => resolve(`slow:${text}`), 2_500)),
      ),
    ),
  };
}

export interface TemporalServer {
  address: string;
  namespace: string;
  teardown(): Promise<void>;
}

/**
 * A fresh Temporal dev server, run from the `temporal` CLI that TEMPORAL_CLI_PATH names (Bazel
 * passes the one pinned in MODULE.bazel, relative to the runfiles root). Never downloads one.
 */
export async function startServer(): Promise<TemporalServer> {
  const cli = process.env.TEMPORAL_CLI_PATH;
  if (!cli) throw new Error("TEMPORAL_CLI_PATH is not set; run the test with Bazel");
  const env = await TestWorkflowEnvironment.createLocal({
    server: { namespace: NAMESPACE, executable: { type: "existing-path", path: resolve(cli) } },
  });
  return { address: env.address, namespace: NAMESPACE, teardown: () => env.teardown() };
}

export interface TestEnv {
  client: Client;
  address: string;
  namespace: string;
  taskQueue: string;
  /** Workflow ids to terminate when the file finishes. */
  started: string[];
  start(workflowType: string, args?: unknown[], workflowId?: string): Promise<WorkflowHandle>;
  /** Run a worker on `taskQueue` (with no workflow cache) until `fn` settles. */
  runWorker<T>(taskQueue: string, fn: () => Promise<T>): Promise<T>;
  /** Stops the worker, then the server. */
  stop(): Promise<void>;
}

/**
 * A dev server and a worker for the fixture workflows; start one per test file in a `before` hook
 * and stop it in `after`. `plugins` shape the workflow bundle only.
 */
export async function startEnv(name: string, plugins?: BundlerPlugin[]): Promise<TestEnv> {
  const server = await startServer();
  const { address, namespace } = server;
  const taskQueue = `test-${name}-${randomUUID()}`;
  const nativeConnection = await NativeConnection.connect({ address });
  const workflowBundle = await bundleWorkflowCode({
    workflowsPath: FIXTURES,
    ...(plugins ? { plugins } : {}),
  });
  const createWorker = (queue: string, maxCachedWorkflows?: number) =>
    Worker.create({
      connection: nativeConnection,
      namespace,
      taskQueue: queue,
      workflowBundle,
      activities: fixtureActivities(),
      interceptors: { activity: [rawTopicAttemptInterceptor] },
      dataConverter,
      ...(maxCachedWorkflows === undefined ? {} : { maxCachedWorkflows }),
    });
  const worker = await createWorker(taskQueue);
  const running = worker.run();
  const connection = await Connection.connect({ address });
  const client = new Client({ connection, namespace, dataConverter });
  const started: string[] = [];
  return {
    client,
    address,
    namespace,
    taskQueue,
    started,
    async start(workflowType, args = [{}], workflowId = `${workflowType}-${randomUUID()}`) {
      const handle = await client.workflow.start(workflowType, { taskQueue, workflowId, args });
      started.push(workflowId);
      return handle;
    },
    async runWorker(queue, fn) {
      return (await createWorker(queue, 0)).runUntil(fn);
    },
    async stop() {
      await Promise.allSettled(
        started.map((id) => client.workflow.getHandle(id).terminate("test finished")),
      );
      worker.shutdown();
      await running;
      await nativeConnection.close();
      await connection.close();
      await server.teardown();
    },
  };
}

export const send = (
  handle: WorkflowHandle,
  type: string,
  payload: Record<string, unknown>,
  updateId?: string,
) =>
  handle.executeUpdate<AgentMessageReply, [unknown]>(SEND_AGENT_MESSAGE_UPDATE, {
    args: [{ type, payload }],
    ...(updateId ? { updateId } : {}),
  });

const isTurnEnd = (e: AgentEvent) => e.event.type === "turn_end";

/** Read an agent's stream from offset 0 until `until` holds, calling `onEvent` for each event. */
export async function collect(
  client: Client,
  workflowId: string,
  {
    until = isTurnEnd,
    onEvent,
  }: { until?: (e: AgentEvent) => boolean; onEvent?: (e: AgentEvent) => unknown } = {},
): Promise<AgentEvent[]> {
  const events: AgentEvent[] = [];
  const items = subscribe<AgentEvent>(client, workflowId, TURN_EVENTS_TOPIC, 0, {
    pollCooldown: "10 milliseconds",
  });
  for await (const item of items) {
    events.push(item.data);
    await onEvent?.(item.data);
    if (until(item.data)) break;
  }
  return events;
}

/** The event types of every event carrying `toolId`, in stream order. */
export const typesFor = (events: AgentEvent[], toolId: string) =>
  events
    .filter((e) => (e.event as { tool_id?: string }).tool_id === toolId)
    .map((e) => e.event.type);

/** The single event of `type`; fails if there is not exactly one. */
export function one<T extends AgentEventType>(events: AgentEvent[], type: T): StreamEventOf<T> {
  const matches = events.filter((e) => e.event.type === type);
  if (matches.length !== 1) throw new Error(`expected exactly one ${type}, got ${matches.length}`);
  return matches[0]!.event as StreamEventOf<T>;
}

/** The text of the first handler reply. */
export const replyText = (events: AgentEvent[]) =>
  (
    (
      events.find((e) => e.event.type === "message_handler_end")!
        .event as StreamEventOf<"message_handler_end">
    ).output as { text: string }
  ).text;

/** Poll `check` until it returns a truthy value. */
export async function eventually<T>(check: () => Promise<T>, attempts = 200): Promise<T> {
  for (let i = 0; ; i++) {
    const value = await check();
    if (value || i >= attempts) return value;
    await new Promise((r) => setTimeout(r, 50));
  }
}

const WIRE_IDS = new Set(["agent_id", "turn_id", "message_id", "workflow_id"]);

/** Wire output with run-specific ids and times replaced, so two runs (or two languages) compare. */
export function normalizeWire(value: unknown): unknown {
  const seen = new Map<string, string>();
  const walk = (v: unknown, key?: string): unknown => {
    if (Array.isArray(v)) return v.map((x) => walk(x));
    if (v !== null && typeof v === "object")
      return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, walk(x, k)]));
    if (key === "timestamp") return 0;
    if (typeof v !== "string" || key === undefined || !WIRE_IDS.has(key)) return v;
    if (!seen.has(v)) seen.set(v, `<${key}:${seen.size}>`);
    return seen.get(v);
  };
  return walk(value);
}

/** Send `ask {text}` to a fresh agent and record its raw wire output: stream, status and interface. */
export async function recordWire(client: Client, workflowId: string, text: string) {
  for await (const _ of await new AgentClient(client, workflowId).sendMessage("ask", { text }));
  const handle = client.workflow.getHandle(workflowId);
  return normalizeWire({
    events: await collect(client, workflowId),
    status: await handle.query(AGENT_STATUS_QUERY),
    interface: await handle.query(AGENT_INTERFACE_QUERY),
  });
}
