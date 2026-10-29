// Rewrites histories/ from fresh runs on a dev server: `bazel run :histories_update`. Run it when
// a change to the workflow code is meant to break replay of the committed histories (nothing
// running on the old code needs to survive it), then commit the new histories.

import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { WorkflowHandle } from "@temporalio/client";
import { historyToJSON } from "@temporalio/common/lib/proto-utils.js";
import { NativeConnection, Worker } from "@temporalio/worker";
import {
  PROVIDE_CALLBACK_RESULT_UPDATE,
  TOOL_APPROVAL_UPDATE,
  type AgentEvent,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { AgentHarnessPlugin } from "../../../src/index.ts";
import { collect, send, startEnv, type TestEnv } from "../../support.ts";
import { BUNDLES, type Bundle } from "./bundles.ts";
import { scriptedDemoActivities, scriptedDemoTools } from "./scripted-demo-activities.ts";

const PACKAGE_PATH = "typescript/packages/worker/test/harness/replay/histories";

async function write(out: string, bundle: Bundle, h: WorkflowHandle, name = h.workflowId) {
  const json = historyToJSON(await h.fetchHistory());
  writeFileSync(
    join(out, bundle, `${name}.json`),
    `${JSON.stringify(JSON.parse(json), null, 1)}\n`,
  );
}

/** A turn, an approval, a callback, Code Mode and a subagent, each its own run of the scripted demo. */
async function recordScriptedDemo(env: TestEnv, out: string) {
  const connection = await NativeConnection.connect({ address: env.address });
  const taskQueue = "scripted-demo";
  const worker = await Worker.create({
    ...BUNDLES["scripted-demo"],
    connection,
    namespace: env.namespace,
    taskQueue,
    activities: scriptedDemoActivities,
    plugins: [new AgentHarnessPlugin({ tools: scriptedDemoTools })],
  });
  // Answer every approval and callback the moment it is asked for.
  const answer = (h: WorkflowHandle) => async (e: AgentEvent) => {
    if (e.event.type === "tool_approval_requested")
      await h.executeUpdate(TOOL_APPROVAL_UPDATE, {
        args: [{ tool_id: e.event.tool_id, approved: true }],
      });
    if (e.event.type === "callback_requested")
      await h.executeUpdate(PROVIDE_CALLBACK_RESULT_UPDATE, {
        args: [{ tool_id: e.event.tool_id, result: { content: "remember the milk" } }],
      });
  };
  const scenarios: [string, string[]][] = [
    ["turn", ["hello", "what's the weather in Paris and search flights"]],
    ["approval", ["book a flight"]],
    ["callback", ["read my file"]],
    ["code-mode-python", ["run some code"]],
    ["subagent", ["echo this back"]],
  ];
  await worker.runUntil(async () => {
    for (const [name, prompts] of scenarios) {
      const h = await env.client.workflow.start("ScriptedDemoAgent", {
        taskQueue,
        workflowId: `scripted-demo-${name}`,
        args: [{}],
      });
      env.started.push(h.workflowId);
      for (const [i, text] of prompts.entries()) {
        await send(h, "ask", { text });
        await collect(env.client, h.workflowId, {
          until: (e) => e.event.type === "turn_end" && e.turn_number === i + 1,
          onEvent: answer(h),
        });
      }
      await h.signal("close");
      await h.result();
      await write(out, "scripted-demo", h);
      for (const event of (await h.fetchHistory()).events ?? []) {
        const childId = event.startChildWorkflowExecutionInitiatedEventAttributes?.workflowId;
        if (!childId) continue;
        const child = env.client.workflow.getHandle(childId);
        await child.result();
        await write(out, "scripted-demo", child, `${h.workflowId}-child`);
      }
    }
  });
  await connection.close();
}


async function main() {
  const workspace = process.env.BUILD_WORKSPACE_DIRECTORY;
  if (!workspace) throw new Error("run this with `bazel run`");
  const out = join(workspace, PACKAGE_PATH);
  const bundles: Bundle[] = ["scripted-demo", "fixtures"];
  for (const bundle of bundles) {
    rmSync(join(out, bundle), { recursive: true, force: true });
    mkdirSync(join(out, bundle), { recursive: true });
  }
  const env = await startEnv("record-histories");
  try {
    await recordScriptedDemo(env, out);
  } finally {
    await env.stop();
  }
  console.log(`Wrote histories to ${out}/`);
}

await main();
