// ABOUTME: Opening a session the session manager does not list — a swarm member or a subagent,
// picked in the Swarm pane or linked by id — falls back to describing that one workflow, and a
// workflow that does not exist is reported rather than opened.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { installBrowserSurface, waitFor } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

const { freshStorage } = installBrowserSurface();

const listed = { workflow_id: "wf-listed", created_at: 1, label: "listed", agent_workflow_type: "X" };

function api(asked) {
  return {
    listAgents: async () => ({ agents: [] }),
    listSessions: async () => [listed],
    agentInterface: async () => [],
    operatorInterface: async () => [],
    workflowStatus: async (workflow_id) => ({ workflow_id, execution_status: "RUNNING", closed: false }),
    async getSession(sessionId) {
      asked.push(sessionId);
      if (sessionId !== "worker-subagent-1") throw new Error("404 workflow_not_found");
      return {
        workflow_id: sessionId,
        created_at: 2,
        label: "worker · EvalTester",
        agent_workflow_type: "EvalTester",
        is_discovered: true,
        execution_status: "RUNNING",
        closed: false
      };
    },
    async *attach(_sessionId, _fromOffset, signal) {
      await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
    }
  };
}

describe("opening an unlisted session", () => {
  it("opens a swarm member by describing its workflow", async () => {
    freshStorage();
    const asked = [];
    const controller = new AgentRunController(api(asked));
    controller.sessions = [listed];

    await controller.selectSession("worker-subagent-1");
    await waitFor("the member to open", () => controller.session?.workflow_id === "worker-subagent-1");
    assert.equal(controller.session.label, "worker · EvalTester");
    assert.deepEqual(asked, ["worker-subagent-1"]);
    assert.equal(controller.sessionsError, null);
  });

  it("does not describe a session the list already has", async () => {
    freshStorage();
    const asked = [];
    const controller = new AgentRunController(api(asked));
    controller.sessions = [listed];
    await controller.selectSession("wf-listed");
    assert.deepEqual(asked, []);
  });

  it("reports an id that names no workflow", async () => {
    freshStorage();
    const controller = new AgentRunController(api([]));
    controller.sessions = [listed];
    await controller.selectSession("nope");
    assert.match(controller.sessionsError, /No session nope/);
  });
});
