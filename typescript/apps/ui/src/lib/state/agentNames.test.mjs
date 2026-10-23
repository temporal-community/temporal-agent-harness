// ABOUTME: Guards that a listed session is named by its agent's registry label, not by its
// workflow type in words, when the list reaches the screen before the registry does.
//
// A reload paints the last session list from its snapshot before `/api/agents` answers, and
// the boot fetch of the registry is never retried if it fails. The Session Manager then read
// "Open AI Hello Agent" for every OpenAI Hello session, in its rows and its agent filter,
// until the New-session picker happened to re-list the agents.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { describe, it } from "vitest";

import { installFakeIndexedDb } from "../../../tests/support/fakeIndexedDb.mjs";
import { installBrowserSurface, session } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";
import { writeStoredActiveSessionId } from "./agentRunStorage.ts";
import { writeView } from "./frameStore.ts";
import { agentName } from "./sessionHistory.ts";

installBrowserSurface();
installFakeIndexedDb();

const registry = [
  { key: "openai-hello", workflow_type: "OpenAIHelloAgent", task_queue: "openai-hello", label: "OpenAI Hello", description: "" },
  { key: "agent-dag", workflow_type: "DagBuilderAgent", task_queue: "agent-dag", label: "Agent DAG Studio", description: "" }
];
const painted = [
  session("wf-hello", { agent_workflow_type: "OpenAIHelloAgent" }),
  session("wf-dag", { agent_workflow_type: "DagBuilderAgent" })
];

describe("agent names in the session list", () => {
  it("come from the registry once it is asked again after a failed boot", async () => {
    await writeView({
      runKey: "wf-hello|2026-10-20T00:00:00Z",
      workflowId: "wf-hello",
      savedAt: 1,
      session: painted[0],
      items: [],
      logs: [],
      sessions: painted,
      total: 0
    });
    writeStoredActiveSessionId("wf-hello");
    let agentCalls = 0;
    const controller = new AgentRunController({
      async listAgents() {
        agentCalls += 1;
        if (agentCalls === 1) throw new Error("limit on number of concurrent in-flight updates");
        return { agents: registry };
      },
      async listSessions() {
        return painted;
      }
    });

    await controller.initialize();
    const names = () => controller.sessions.map((item) => agentName(controller.agents, item.agent_workflow_type));
    assert.equal(controller.connecting, false);
    assert.deepEqual(names(), ["Open AI Hello Agent", "Dag Builder Agent"], "the painted list outlives the failed registry fetch");

    /* What App's effect does once boot settles with a list and no registry. */
    await controller.ensureAgents();
    assert.deepEqual(names(), ["OpenAI Hello", "Agent DAG Studio"]);
  });

  it("are asked for by the app whenever a list is shown without a registry", async () => {
    const app = await readFile(new URL("../../App.svelte", import.meta.url), "utf8");
    assert.match(
      app,
      /\$effect\(\(\) => \{\s*if \(run\.sessions\.length === 0 \|\| run\.connecting\) return;\s*untrack\(\(\) => \{\s*if \(run\.agents\.length === 0\) void run\.ensureAgents\(\);/,
      "App must re-ask for the registry when sessions are listed without it, with the registry untracked"
    );
  });
});
