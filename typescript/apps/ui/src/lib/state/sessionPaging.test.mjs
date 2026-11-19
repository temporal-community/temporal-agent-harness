import assert from "node:assert/strict";
import { beforeAll, describe, it } from "vitest";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

const at = (workflow_id, created_at) => ({
  workflow_id,
  agent_workflow_type: "QaAgentWorkflow",
  label: "QA",
  execution_status: "RUNNING",
  closed: false,
  created_at
});

const page = (sessions, next_page_token = null) => ({ sessions, next_page_token });
const ids = (controller) => controller.sessions.map((session) => session.workflow_id);

/* The server lists sessions a page at a time, newest first, from Temporal visibility. */
describe("session pages", () => {
  beforeAll(() => installBrowserSurface());

  function controllerOver(api) {
    return new AgentRunController({
      async listAgents() {
        return { agents: [{ key: "qa", label: "QA", workflow_type: "QaAgentWorkflow", task_queue: "q", description: "" }] };
      },
      async agentInterface() {
        return [];
      },
      async workflowStatus(workflow_id) {
        return { workflow_id, execution_status: "RUNNING", closed: false };
      },
      // eslint-disable-next-line require-yield
      async *attach() {},
      ...api
    });
  }

  it("loads older pages on request, until there are none", async () => {
    const asked = [];
    const controller = controllerOver({
      async listSessions(pageToken) {
        asked.push(pageToken ?? null);
        return pageToken === "p2" ? page([at("wf-1", 1)]) : page([at("wf-3", 3), at("wf-2", 2)], "p2");
      }
    });

    await controller.refreshSessions();
    assert.deepEqual(ids(controller), ["wf-3", "wf-2"]);
    assert.equal(controller.sessionsNextPageToken, "p2");

    await controller.loadOlderSessions();
    assert.deepEqual(ids(controller), ["wf-3", "wf-2", "wf-1"]);
    assert.equal(controller.sessionsNextPageToken, null);
    await controller.loadOlderSessions();
    assert.deepEqual(asked, [null, "p2"], "no page is asked for past the last");
  });

  it("keeps older pages and a session it just created across a re-list", async () => {
    let first = page([at("wf-3", 3), at("wf-2", 2)], "p2");
    const controller = controllerOver({
      async listSessions(pageToken) {
        return pageToken === "p2" ? page([at("wf-1", 1)], "p3") : first;
      },
      async createSession() {
        return at("wf-new", 4);
      }
    });
    await controller.refreshSessions();
    await controller.loadOlderSessions();
    await controller.startNewSession("QaAgentWorkflow");

    // Visibility has not caught up with the new session yet.
    await controller.refreshSessions();
    assert.deepEqual(ids(controller), ["wf-new", "wf-3", "wf-2", "wf-1"]);
    assert.equal(controller.sessionsNextPageToken, "p3", "the list carries on past the pages it holds");

    // Once a list carries it, the list is what says whether it is still there.
    first = page([at("wf-new", 4), at("wf-3", 3)], "p2");
    await controller.refreshSessions();
    first = page([at("wf-3", 3), at("wf-1", 1)], "p3");
    await controller.refreshSessions();
    assert.deepEqual(ids(controller), ["wf-3", "wf-1"], "wf-new and wf-2 are missing from a list that spans them");
  });

  it("drops what the last page leaves out", async () => {
    let sessions = [at("wf-2", 2), at("wf-1", 1)];
    const controller = controllerOver({
      async listSessions() {
        return page(sessions);
      }
    });
    await controller.refreshSessions();
    sessions = [at("wf-2", 2)];
    await controller.refreshSessions();
    assert.deepEqual(ids(controller), ["wf-2"]);
  });

  it("opens a session on a page it has not loaded by asking for it by id", async () => {
    const old = at("wf-old", 1);
    const controller = controllerOver({
      async listSessions() {
        return page([at("wf-new", 2)], "p2");
      },
      async getSession(sessionId) {
        return sessionId === old.workflow_id ? old : null;
      }
    });
    await controller.refreshSessions();

    await controller.selectSession("wf-old");
    assert.equal(controller.session?.workflow_id, "wf-old");
    assert.deepEqual(ids(controller), ["wf-new", "wf-old"]);

    await controller.selectSession("wf-missing");
    assert.match(controller.sessionsError ?? "", /No session wf-missing/);
  });
});
