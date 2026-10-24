// AgentHarnessPlugin (port of tests/plugin/test_plugin.py, minus the Python-only data converter
// and payload offload cases): what it registers, and that a worker's own activities win.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { after, before, describe, it } from "node:test";
import { ApplicationFailure } from "@temporalio/common";
import {
  NativeConnection,
  Worker,
  bundleWorkflowCode,
  type WorkerOptions,
} from "@temporalio/worker";
import { AgentClient } from "../src/harness/agent-client.ts";
import { codeModeActivities } from "../src/harness/code-mode/monty-stepper.ts";
import type { AgentEvent } from "@temporalio/agent-harness/harness/agent-protocol";
import { AgentHarnessPlugin, CODE_MODE_MISSING_EXTRA_ERROR } from "../src/index.ts";
import { assertMatch } from "./match.ts";
import {
  FIXTURES,
  TEST_CONTINUATION_SECRET,
  collect,
  replyText,
  send,
  startEnv,
  type TestEnv,
} from "./support.ts";

const CODE_MODE = ["code_start_batch", "code_resume_batch", "code_type_check"];
const HARNESS = ["run_subagent_turn", "jev_tool_approval", ...CODE_MODE];

const registered = (activities?: object, plugin = new AgentHarnessPlugin()) =>
  plugin.configureWorker({
    taskQueue: "plugin-test",
    ...(activities ? { activities } : {}),
  } as WorkerOptions).activities as Record<string, unknown>;

describe("AgentHarnessPlugin", () => {
  it("gives a worker every harness activity, Code Mode's included", () => {
    assert.deepStrictEqual(Object.keys(registered()).sort(), [...HARNESS].sort());
  });

  it("lets a worker's own registration of a harness activity win", () => {
    const mine = async () => "mine";
    const activities = registered({ code_start_batch: mine, run_subagent_turn: mine });
    assert.equal(activities.code_start_batch, mine);
    assert.equal(activities.run_subagent_turn, mine);
    assert.deepStrictEqual(Object.keys(activities).sort(), [...HARNESS].sort());
  });

  it("keeps unrelated worker activities, and adds extraActivities", () => {
    const unrelated = async () => undefined;
    const extra = async () => undefined;
    const activities = registered(
      { unrelated },
      new AgentHarnessPlugin({ extraActivities: { extra } }),
    );
    assert.deepStrictEqual(
      Object.keys(activities).sort(),
      ["extra", "unrelated", ...HARNESS].sort(),
    );
  });

  it("registers Code Mode stubs that fail once, non-retryably, saying what to register", async () => {
    const stub = registered().code_start_batch as () => Promise<never>;
    const failure = await stub().catch((e: unknown) => e);
    assert.ok(failure instanceof ApplicationFailure);
    assertMatch(failure, { type: CODE_MODE_MISSING_EXTRA_ERROR, nonRetryable: true });
    assert.ok((failure as Error).message.includes("codeModeActivities()"));
  });
});

describe("a Code Mode call on a worker without Code Mode", () => {
  let env: TestEnv;
  let worker: Worker;
  let running: Promise<void>;
  let connection: NativeConnection;
  const taskQueue = `plugin-no-code-mode-${randomUUID()}`;
  before(async () => {
    env = await startEnv("plugin");
    connection = await NativeConnection.connect({ address: env.address });
    worker = await Worker.create({
      connection,
      namespace: env.namespace,
      taskQueue,
      workflowsPath: FIXTURES,
      plugins: [new AgentHarnessPlugin()],
    });
    running = worker.run();
  });
  after(async () => {
    worker.shutdown();
    await running;
    await connection.close();
    await env.stop();
  });

  it("fails the tool call at once instead of hanging the turn", async () => {
    const workflowId = `code-mode-missing-${randomUUID()}`;
    await env.client.workflow.start("CodeModeProbeAgent", { taskQueue, workflowId, args: [{}] });
    env.started.push(workflowId);
    const events: AgentEvent["event"][] = [];
    for await (const [item] of await new AgentClient(env.client, workflowId).sendMessage("run", {
      text: "1 + 1",
    })) {
      events.push((item as AgentEvent).event);
    }
    assert.equal(events.filter((e) => e?.type === "tool_error").length, 1);
    assert.equal(events.at(-1)!.type, "turn_end");
    const failed = (await env.client.workflow.getHandle(workflowId).fetchHistory()).events!.flatMap(
      (e) => (e.activityTaskFailedEventAttributes ? [e.activityTaskFailedEventAttributes] : []),
    );
    assert.equal(failed.length, 1);
    assert.ok(failed[0]!.failure!.message?.includes("ran on a worker without its sandbox"));
    assertMatch(failed[0]!.failure!.applicationFailureInfo, {
      type: CODE_MODE_MISSING_EXTRA_ERROR,
      nonRetryable: true,
    });
  });

  it("a worker stopping leaves the process's Monty pool open, so Code Mode still runs on another worker", async () => {
    const montyChildren = () => {
      try {
        return execFileSync("pgrep", ["-P", String(process.pid), "-f", "monty"], {
          encoding: "utf8",
        })
          .trim()
          .split("\n");
      } catch {
        return [];
      }
    };
    const workflowBundle = await bundleWorkflowCode({ workflowsPath: FIXTURES });
    const codeModeWorker = async () => {
      const taskQueue = `plugin-pool-${randomUUID()}`;
      const worker = await Worker.create({
        connection,
        namespace: env.namespace,
        taskQueue,
        workflowBundle,
        activities: codeModeActivities({ continuationSecret: TEST_CONTINUATION_SECRET }),
        plugins: [new AgentHarnessPlugin()],
      });
      return { taskQueue, worker, running: worker.run() };
    };
    const runCode = async (taskQueue: string) => {
      const workflowId = `code-mode-pool-${randomUUID()}`;
      const handle = await env.client.workflow.start("CodeModeProbeAgent", {
        taskQueue,
        workflowId,
        args: [{}],
      });
      env.started.push(workflowId);
      await send(handle, "run", { text: "1 + 1" });
      return replyText(await collect(env.client, workflowId));
    };
    const first = await codeModeWorker();
    const second = await codeModeWorker();
    try {
      assert.equal(await runCode(first.taskQueue), "result: 2");
      first.worker.shutdown();
      await first.running;
      assert.ok(montyChildren().length > 0);
      assert.equal(await runCode(second.taskQueue), "result: 2");
    } finally {
      if (first.worker.getState() === "RUNNING") first.worker.shutdown();
      second.worker.shutdown();
      await Promise.allSettled([first.running, second.running]);
    }
  });
});
