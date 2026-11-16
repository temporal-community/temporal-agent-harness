import assert from "node:assert/strict";
import { afterEach, beforeAll, describe, it } from "vitest";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

const at = (workflow_id, over = {}) => ({
  workflow_id,
  agent_workflow_type: "QaAgentWorkflow",
  is_message_queuing_enabled: true,
  execution_status: "RUNNING",
  closed: false,
  created_at: 0,
  ...over
});

const gone = { execution_status: "NOT_FOUND", closed: true };
const done = { execution_status: "COMPLETED", closed: true };

/*
 * Boot opens the session the address bar names and nothing else. Without one the console
 * lists the sessions and agents to choose from, the way Temporal's own UI lists workflows
 * before you open one: a running session, a remembered one or the default agent's latest
 * are all things to pick from that list, not places to land.
 */
describe("boot", () => {
  let freshStorage;

  beforeAll(() => {
    ({ freshStorage } = installBrowserSurface());
  });

  afterEach(() => {
    window.location.search = "";
  });

  // eslint-disable-next-line require-yield
  async function* silent() {
    return;
  }

  async function bootWith(sessions, link = null) {
    freshStorage();
    if (link) window.location.search = `?s=${link}`;
    const created = [];
    const controller = new AgentRunController({
      async listAgents() {
        return {
          agents: [{ key: "qa", label: "QA", workflow_type: "QaAgentWorkflow", task_queue: "q", description: "" }]
        };
      },
      async acceptedMessageTypes() {
        return { accepted: [] };
      },
      async listSessions() {
        return sessions;
      },
      async createSession() {
        const fresh = at("wf-fresh");
        created.push(fresh);
        return fresh;
      },
      async agentInterface() {
        return [];
      },
      async operatorInterface() {
        return [];
      },
      async workflowStatus(workflowId) {
        const row = sessions.find((item) => item.workflow_id === workflowId);
        const status = row?.execution_status ?? "RUNNING";
        return { workflow_id: workflowId, execution_status: status, closed: status !== "RUNNING" };
      },
      attach: () => silent()
    });
    await controller.initialize();
    return { controller, created };
  }

  it("opens the session the address bar names, live or closed", async () => {
    for (const id of ["wf-live", "wf-done"]) {
      const { controller, created } = await bootWith([at("wf-live"), at("wf-done", done)], id);
      assert.equal(controller.session?.workflow_id, id, `boot did not open ${id} (said: ${controller.connectionError ?? "nothing"})`);
      assert.equal(created.length, 0);
    }
  });

  it("opens nothing without one, even with sessions running, and lists them", async () => {
    const sessions = [at("wf-a"), at("wf-b", { created_at: 5 }), at("wf-done", done)];
    const { controller, created } = await bootWith(sessions);
    assert.equal(controller.session, null);
    assert.equal(created.length, 0, "loading the page must never start a workflow");
    assert.equal(controller.connecting, false);
    assert.equal(controller.connectionError, null);
    assert.deepEqual(controller.sessions.map((s) => s.workflow_id), ["wf-a", "wf-b", "wf-done"]);
    assert.equal(controller.agents.length, 1, "the agents to start are listed too");
  });

  it("does not park on a purged session the address bar names, and starts nothing in its place", async () => {
    const { controller, created } = await bootWith([at("wf-gone", gone), at("wf-live")], "wf-gone");
    assert.equal(controller.session, null);
    assert.equal(created.length, 0);
  });

  /* A link to a session the session manager doesn't list, mistyped or purged, says so instead of
     landing on the lists without a word, and drops the id so a reload doesn't land there again. */
  it("names a linked session the session manager doesn't list, and drops it from the address bar", async () => {
    const urls = [];
    window.history.replaceState = (_state, _title, url) => urls.push(String(url));
    window.location.href = "http://localhost/?s=wf-nope";
    try {
      const { controller, created } = await bootWith([at("wf-live")], "wf-nope");
      assert.equal(controller.session, null);
      assert.equal(created.length, 0);
      assert.equal(controller.missingLinkedSession, "wf-nope");
      assert.ok(urls.length > 0 && urls.every((url) => !url.includes("s=")), `the id stayed in the address bar: ${urls.join(", ")}`);
    } finally {
      window.location.href = "http://localhost/";
      window.history.replaceState = () => {};
    }
  });

  /* Home leaves the session's workflow as it is: only the view closes, and the link with it. */
  it("closes the open session's view without ending it, and drops it from the address bar", async () => {
    const { controller } = await bootWith([at("wf-live")], "wf-live");
    assert.equal(controller.session?.workflow_id, "wf-live");
    const urls = [];
    window.history.replaceState = (_state, _title, url) => urls.push(String(url));
    window.location.href = "http://localhost/?s=wf-live";
    try {
      controller.closeSessionView();
      assert.equal(controller.session, null);
      assert.equal(controller.connecting, false);
      assert.deepEqual(controller.closedWorkflowIds, [], "the workflow is not ended");
      assert.ok(urls.length === 1 && !urls[0].includes("s="));
    } finally {
      window.location.href = "http://localhost/";
      window.history.replaceState = () => {};
    }
  });

  /* Marking closed sessions one at a time copied the whole list per id: 10,000 sessions
     froze the tab for 88 s. At 20,000 here, quadratic work takes ~2.8 s and linear ~6 ms. */
  it("marks every closed session of a 20,000-session list in linear time", async () => {
    const sessions = Array.from({ length: 20_000 }, (_, i) => at(`wf-${i}`, done));
    const started = performance.now();
    const { controller } = await bootWith(sessions);
    const elapsed = performance.now() - started;
    assert.equal(controller.closedWorkflowIds.length, 20_000, "every closed session should be marked closed");
    assert.ok(elapsed < 1_000, `booting 20,000 sessions took ${Math.round(elapsed)} ms`);
  });
});
