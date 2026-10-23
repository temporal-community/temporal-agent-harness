/**
 * A caught-up session must still notice new turns.
 *
 * /api/attach ends at quiescence, and the controller stops re-reading a stream it
 * has caught up with (see caughtUpAttach.test.mjs). Ending there for good left a
 * second tab, or a tab watching a session another client drives, frozen on the
 * last turn it saw. Instead the stream waits: a send from any tab in this browser
 * wakes it over a BroadcastChannel, and a slow poll of the agent's status, made
 * only while the page is shown, notices a turn some other client started.
 */
import assert from "node:assert/strict";
import { afterEach, describe, it } from "vitest";

import { installBrowserSurface, sleep, waitFor } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

const { freshStorage } = installBrowserSurface();

const session = (id) => ({
  workflow_id: id,
  agent_workflow_type: "IncidentCommander",
  run_id: `${id}-run`,
  execution_status: "RUNNING",
  closed: false
});

/** One agent's event log, as every tab's /api/attach reads it. */
function server() {
  const log = [];
  const status = { current_turn: 0, turn_active: false, pending_turns: [] };
  const turn = (n) => {
    const at = log.length;
    const envelope = (type, extra = {}) => ({
      event: type,
      data: {
        type,
        agent_id: "root",
        turn_id: `t${n}`,
        turn_number: n,
        message_id: `m${n}`,
        timestamp: log.length,
        resume_offset: log.length + 1,
        event_offset: log.length,
        ...extra
      }
    });
    log.push(envelope("turn_started"));
    log.push(envelope("reply_delta", { delta: `reply ${n}` }));
    log.push(envelope("message_handler_end", { output: { text: `reply ${n}` } }));
    log.push(envelope("turn_end"));
    status.current_turn = n;
    return at;
  };
  return { log, status, turn };
}

function tab(srv) {
  const attaches = [];
  let statusAsks = 0;
  const api = {
    async agentStatus() {
      statusAsks += 1;
      return { ...srv.status };
    },
    async listAgents() {
      return { agents: [] };
    },
    async listSessions() {
      return [];
    },
    async agentInterface() {
      return [];
    },
    async operatorInterface() {
      return [];
    },
    async workflowStatus(workflowId) {
      return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
    },
    async submitMessage() {
      srv.turn(srv.status.current_turn + 1);
      return { turn_number: srv.status.current_turn, turn_id: "t", message_id: `m${srv.status.current_turn}`, accepted_offset: 0, disposition: "opened" };
    },
    async *attach(_id, fromOffset) {
      attaches.push(fromOffset);
      for (const frame of srv.log.slice(fromOffset)) yield frame;
    }
  };
  const controller = new AgentRunController(api);
  controller.sessions = [session("wf")];
  return { controller, attaches, statusAsks: () => statusAsks };
}

const replies = (controller) => controller.frames.filter((f) => f.event === "message_handler_end").length;

afterEach(() => {
  delete globalThis.document;
});

describe("a caught-up session", () => {
  it("follows a turn another tab on the same session sends", async () => {
    freshStorage();
    const srv = server();
    srv.turn(1);
    const a = tab(srv);
    const b = tab(srv);
    void a.controller.selectSession("wf");
    void b.controller.selectSession("wf");
    // A read that delivered is read once more, after the first 500ms backoff.
    await waitFor("both tabs to catch up", () => a.attaches.length === 2 && b.attaches.length === 2);
    await sleep(100);
    assert.deepEqual(b.attaches, [0, 4], "fixture: the second tab is caught up and quiet");

    await a.controller.sendMessage({ text: "hello" });
    await waitFor("the other tab to show the new turn", () => replies(b.controller) === 2);
    assert.deepEqual(b.attaches, [0, 4, 4], "it re-attaches once, from where it stopped");
  });

  it("notices a turn another client started, polling only while the page is shown", async () => {
    freshStorage();
    const page = Object.assign(new EventTarget(), { hidden: false });
    globalThis.document = page;
    const srv = server();
    srv.turn(1);
    const t = tab(srv);
    t.controller.idlePollMs = 20;
    void t.controller.selectSession("wf");
    await waitFor("the tab to catch up", () => t.attaches.length === 2);
    const before = t.statusAsks();
    await waitFor("the idle poll to ask", () => t.statusAsks() >= before + 3);
    assert.deepEqual(t.attaches, [0, 4], "a poll that finds nothing new does not re-attach");

    page.hidden = true;
    page.dispatchEvent(new Event("visibilitychange"));
    await sleep(40);
    const asked = t.statusAsks();
    await sleep(120);
    assert.equal(t.statusAsks(), asked, "a hidden page does not poll");

    srv.turn(2);
    page.hidden = false;
    page.dispatchEvent(new Event("visibilitychange"));
    await waitFor("the external turn to show", () => replies(t.controller) === 2, 1_000);
    assert.deepEqual(t.attaches, [0, 4, 4]);
  });
});
