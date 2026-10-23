import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { installFakeIndexedDb } from "../../../tests/support/fakeIndexedDb.mjs";
import {
  installBrowserSurface,
  session,
  sleep,
  variedFrame,
  waitFor
} from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";
import { writeStoredActiveSessionId } from "./agentRunStorage.ts";
import { appendRunFrames, readLatestView, runKeyOf, writeView } from "./frameStore.ts";

const { listeners } = installBrowserSurface();
const { stores } = installFakeIndexedDb();

/** An API whose attach replays `frames` for any session, then holds the stream open. */
function api(frames) {
  return {
    async listAgents() {
      return { agents: [] };
    },
    async listSessions() {
      return [];
    },
    async agentInterface() {
      return [];
    },
    async workflowStatus(workflowId) {
      return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
    },
    async *attach(_id, fromOffset, signal) {
      for (const frame of frames) if (frame.data.resume_offset > fromOffset) yield frame;
      await new Promise((resolve) => signal?.addEventListener("abort", resolve));
    }
  };
}

describe("the reload snapshot", () => {
  it("is written when the page goes away, before the debounced write would have run", async () => {
    const controller = new AgentRunController(api([1, 2, 3].map((i) => variedFrame(i, { replay: false }))));
    controller.sessions = [session("wf-leaving")];
    void controller.selectSession("wf-leaving");
    await waitFor("the frames to stream in", () => controller.frames.length === 3);

    for (const leave of listeners.get("pagehide") ?? []) leave();
    await sleep(20);

    assert.equal(stores.get("frames").rows.size, 3, "the staged frames must be written");
    assert.equal(stores.get("views").rows.size, 1, "and the view a reload paints first");
  });

  const view = (workflowId) => ({
    runKey: `${workflowId}|2026-10-19T00:00:00Z`,
    workflowId,
    savedAt: 1,
    session: session(workflowId),
    items: [],
    logs: [],
    sessions: [],
    total: 0
  });

  it("is never another workflow's whose id only starts with this one and a `|`", async () => {
    await writeView(view("wf-a|b"));
    assert.equal(await readLatestView("wf-a"), null);
    assert.equal((await readLatestView("wf-a|b"))?.workflowId, "wf-a|b");
  });

  it("keeps its opened session up when the lists fail, and leaves the failure to the picker", async () => {
    await writeView(view("wf-painted"));
    writeStoredActiveSessionId("wf-painted");
    const controller = new AgentRunController(api([]));
    await controller.initialize();
    await waitFor("the list failure", () => controller.sessionsError !== null);

    assert.equal(controller.sessionsError, "No agent is registered.");
    assert.equal(controller.connectionError, null, "the open session did not fail");
    assert.equal(controller.session?.workflow_id, "wf-painted");
  });

  /* The lists are Session Manager queries. A reload used to wait on them before restoring the
     run or opening its stream, so while they hung only the painted chat showed, "Connecting". */
  it("restores the run and resumes its stream while the Session Manager's lists hang", async () => {
    const stored = session("wf-reloaded", { created_at: 7 });
    const frames = [1, 2, 3].map((offset) => ({
      event: "reply_delta",
      data: {
        type: "reply_delta",
        agent_id: "root",
        turn_id: "t1",
        turn_number: 1,
        message_id: "m1",
        timestamp: offset,
        resume_offset: offset,
        event_offset: offset - 1,
        delta: `#${offset} `,
        replay: false
      }
    }));
    const record = {
      runKey: runKeyOf(stored),
      workflowId: stored.workflow_id,
      lastResumeOffset: 3,
      offsets: {},
      count: 3,
      lastAccess: Date.now()
    };
    await appendRunFrames(record, 0, frames);
    await writeView({ ...view("wf-reloaded"), runKey: runKeyOf(stored), session: stored, total: 3 });
    writeStoredActiveSessionId("wf-reloaded");

    const attachedFrom = [];
    const hang = () => new Promise(() => {});
    const controller = new AgentRunController({
      listAgents: hang,
      listSessions: hang,
      async agentInterface() {
        return [];
      },
      async workflowStatus(workflowId) {
        return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
      },
      async agentStatus() {
        return { turn_active: false, pending_turns: [], current_turn: 1 };
      },
      async *attach(_id, fromOffset) {
        attachedFrom.push(fromOffset);
      }
    });
    controller.idlePollMs = 0;
    void controller.initialize();

    await waitFor("the stored frames", () => controller.frames.length === 3);
    await waitFor("the stream", () => attachedFrom.length === 1);
    assert.equal(attachedFrom[0], 3, "the stream resumes past what was restored");
    await waitFor("the stream to go idle", () => !controller.connecting);
    assert.equal(controller.connectingSince, null);
  });

  /* A view is optional. The frames and the active session id are what a reload
     has whenever the run was written; waiting on /api/agents before folding them
     left the console empty until a session click took the switch path. */
  it("projects a stored run on reload without a session switch, then switches", async () => {
    const reply = (workflowId, text, offset) => ({
      event: "reply_delta",
      data: {
        type: "reply_delta",
        agent_id: "root",
        turn_id: "t1",
        turn_number: 1,
        message_id: `m-${workflowId}`,
        timestamp: offset,
        resume_offset: offset,
        event_offset: offset - 1,
        text,
        replay: false
      }
    });
    const cached = session("wf-cached", { created_at: 9 });
    const other = session("wf-other", { created_at: 4 });
    const store = async (row, text) => {
      await appendRunFrames(
        {
          runKey: runKeyOf(row),
          workflowId: row.workflow_id,
          lastResumeOffset: 1,
          offsets: {},
          count: 1,
          lastAccess: Date.now()
        },
        0,
        [reply(row.workflow_id, text, 1)]
      );
    };
    await store(cached, "cached reply");
    await store(other, "other reply");
    writeStoredActiveSessionId("wf-cached");

    const switched = [];
    const controller = new AgentRunController({
      listAgents: () => new Promise(() => {}),
      async listSessions() {
        return [cached, other];
      },
      async agentInterface() {
        return [];
      },
      async workflowStatus(workflowId) {
        return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
      },
      async *attach() {}
    });
    const selectSession = controller.selectSession.bind(controller);
    controller.selectSession = async (sessionId) => {
      switched.push(sessionId);
      return selectSession(sessionId);
    };
    controller.idlePollMs = 0;
    void controller.initialize();

    await waitFor("the cached projection", () =>
      controller.chatView.items.some((item) => item.text?.includes("cached reply"))
    );
    assert.equal(switched.length, 0, "reload must project without a session switch");
    assert.equal(controller.session?.workflow_id, "wf-cached");
    assert.equal(controller.frames.length, 1);
    assert.ok(controller.graph.nodes.some((node) => node.id.endsWith("::output")));
    assert.ok(controller.stepTimeline.turns.length > 0);

    await controller.selectSession("wf-other");
    await waitFor("the other session", () =>
      controller.chatView.items.some((item) => item.text?.includes("other reply"))
    );
    assert.equal(controller.graph.nodes.some((node) => node.id.startsWith("wf-other::")), true);

    await controller.selectSession("wf-cached");
    await waitFor("the cached session again", () =>
      controller.chatView.items.some((item) => item.text?.includes("cached reply"))
    );
    assert.ok(controller.graph.nodes.some((node) => node.id === "wf-cached::output"));
    assert.deepEqual(switched, ["wf-other", "wf-cached"]);
  });
});
