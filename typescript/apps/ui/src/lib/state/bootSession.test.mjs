import assert from "node:assert/strict";
import { beforeAll, describe, it } from "vitest";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";
import { writeStoredActiveSessionId } from "./agentRunStorage.ts";
import { chooseBootSession } from "./bootSession.ts";

const at = (workflow_id, over = {}) => ({
  workflow_id,
  agent_workflow_type: "QaAgentWorkflow",
  is_message_queuing_enabled: true,
  execution_status: "RUNNING",
  closed: false,
  /* Same age unless a case says otherwise, so no fixture is accidentally newer
     than another and the claims below are the only thing ordering them. */
  created_at: 0,
  ...over
});

/* The session list's own answer to "has anything been said here": the preview
   the agent writes on its first message. Nothing to preview, nothing to show. */
const spokenTo = { initial_user_message: "how do I book a flight" };

const gone = { execution_status: "NOT_FOUND", closed: true };
const done = { execution_status: "COMPLETED", closed: true };

const live = at("live");
const gone1 = at("gone1", gone);
const done1 = at("done1", done);

describe("chooseBootSession", () => {
  it("prefers the stored session while it is live", () => {
    assert.equal(
      chooseBootSession([live, done1], "live", "QaAgentWorkflow"),
      live,
      "the stored session should win when it is live"
    );
  });

  it("reopens a completed stored session, whose frames are cached", () => {
    assert.equal(
      chooseBootSession([live, done1], "done1", "QaAgentWorkflow"),
      done1,
      "a completed stored session should still reopen, since its frames are cached"
    );
  });

  it("skips a purged stored session", () => {
    assert.equal(
      chooseBootSession([live, gone1], "gone1", "QaAgentWorkflow"),
      live,
      "a NOT_FOUND stored session should be skipped for a live one"
    );
    assert.equal(
      chooseBootSession([gone1, done1], "gone1", "QaAgentWorkflow"),
      null,
      "with nothing live left, boot should start a fresh session rather than a dead one"
    );
  });

  it("never auto-boots a closed session", () => {
    assert.equal(
      chooseBootSession([done1, gone1], null, "QaAgentWorkflow"),
      null,
      "auto-boot should never pick a closed session: attach is skipped and there is no cache"
    );
  });

  it("ignores sessions belonging to another agent", () => {
    assert.equal(
      chooseBootSession(
        [at("other", { agent_workflow_type: "OtherWorkflow" })],
        null,
        "QaAgentWorkflow"
      ),
      null,
      "auto-boot should ignore sessions belonging to another agent"
    );
  });

  it("auto-boots the most recent live session, and nothing from an empty list", () => {
    const newer = at("newer");
    assert.equal(
      chooseBootSession([live, newer], null, "QaAgentWorkflow"),
      newer,
      "auto-boot should prefer the most recent live session"
    );
    assert.equal(
      chooseBootSession([], null, "QaAgentWorkflow"),
      null,
      "an empty session list should start a fresh session"
    );
  });
});

/* The console this came from: a probe run left eight untouched sessions behind,
   a browser was parked on one of them, and every reload reopened it — 0 rows,
   scrubber max 0 — while the sessions created after it had events to show.
   Asserted as consequences: which session comes back, never how emptiness or
   age is read off the row. */
describe("a remembered session with nothing in it", () => {
  const emptyOld = at("empty-old", { created_at: 100 });
  const busyNew = at("busy-new", { created_at: 200, ...spokenTo });

  it("is left behind once newer ones exist", () => {
    assert.equal(
      chooseBootSession([emptyOld, busyNew], "empty-old", "QaAgentWorkflow"),
      busyNew,
      "a remembered session nobody has spoken to should be left behind once newer ones exist"
    );
  });

  it("still wins when it is the newest, since that is the one just created", () => {
    const emptyNewest = at("empty-newest", { created_at: 300 });
    assert.equal(
      chooseBootSession([busyNew, emptyNewest], "empty-newest", "QaAgentWorkflow"),
      emptyNewest,
      "emptiness alone must not lose the session: the newest one is the one just created on purpose"
    );
  });

  it("is not lost to age alone when it has events to scrub", () => {
    const busyOld = at("busy-old", { created_at: 100, ...spokenTo });
    assert.equal(
      chooseBootSession([busyOld, at("newer", { created_at: 400 })], "busy-old", "QaAgentWorkflow"),
      busyOld,
      "age alone must not lose the session either: it has events to scrub through"
    );
  });

  /* Nothing else live to fall to, so the empty session is still the best thing on
     offer — and reopening it beats manufacturing yet another session, which is
     how eight of them came to be there in the first place. */
  it("is reopened rather than replaced when it is all there is", () => {
    assert.equal(
      chooseBootSession(
        [emptyOld, at("newer-done", { created_at: 200, ...done })],
        "empty-old",
        "QaAgentWorkflow"
      ),
      emptyOld,
      "falling through must not create a ninth session when the empty one is all there is"
    );
  });

  /* Closed as well as contentless and superseded: nothing to stream, nothing
     cached, nothing to scrub. That one is worth a fresh session. */
  it("hands back a fresh start once it is closed as well", () => {
    assert.equal(
      chooseBootSession(
        [
          at("empty-old-done", { created_at: 100, ...done }),
          at("newer-done", { created_at: 200, ...done })
        ],
        "empty-old-done",
        "QaAgentWorkflow"
      ),
      null,
      "a closed, contentless, superseded session should hand back a fresh start"
    );
  });

  /* A closed session with a transcript still reopens, newer sessions or not: its
     frames are cached, which is the rule the second case in this file pins and
     the one an emptiness test is most likely to swallow. */
  it("does not swallow a closed session that has a transcript", () => {
    const doneBusy = at("done-busy", { created_at: 100, ...done, ...spokenTo });
    assert.equal(
      chooseBootSession(
        [doneBusy, at("newer", { created_at: 200 })],
        "done-busy",
        "QaAgentWorkflow"
      ),
      doneBusy,
      "a completed stored session with a transcript should still reopen"
    );
  });
});

/*
 * Everything above asks chooseBootSession directly, and that is how a NOT_FOUND
 * session shipped anyway: initialize() asks chooseBootSession, which declines the
 * purged id, and then hands the very same id to #resolveSessionById as a deep-link
 * fallback. That helper searched the list on workflow_id alone, so it returned the
 * row the line above had just rejected and the console parked on a dead workflow on
 * every reload. Two functions, one rule, and a unit test on either one passes.
 *
 * So this drives the real controller.
 */
describe("initialize() honours the rule chooseBootSession states", () => {
  let freshStorage;

  beforeAll(() => {
    ({ freshStorage } = installBrowserSurface());
  });

  // eslint-disable-next-line require-yield
  async function* silent() {
    return;
  }

  async function bootWith(sessions, storedId) {
    freshStorage();
    writeStoredActiveSessionId(storedId);
    const created = [];
    const controller = new AgentRunController({
      async listAgents() {
        return {
          agents: [
            {
              key: "qa",
              label: "QA",
              workflow_type: "QaAgentWorkflow",
              task_queue: "q",
              description: ""
            }
          ]
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
        return {
          workflow_id: workflowId,
          execution_status: status,
          closed: status !== "RUNNING"
        };
      },
      attach: () => silent()
    });
    await controller.initialize();
    return { controller, created };
  }

  /* Positive control first: without it, a boot that fell over for any reason at all
     would satisfy the claim below by never opening anything. */
  it("reopens a live stored session", async () => {
    const { controller } = await bootWith([at("wf-live")], "wf-live");
    assert.equal(
      controller.session?.workflow_id,
      "wf-live",
      `a live stored session must still reopen through initialize() (boot said: ${controller.connectionError ?? "nothing"})`
    );
  });

  it("does not park on a purged session, and starts a fresh one instead", async () => {
    const { controller, created } = await bootWith([at("wf-gone", gone)], "wf-gone");
    assert.notEqual(
      controller.session?.workflow_id,
      "wf-gone",
      "initialize() must not park on a purged session that chooseBootSession already declined"
    );
    assert.equal(created.length, 1, "with nothing openable left, boot must start a fresh session");
  });
});
