// ABOUTME: A send the server rejects must tell the composer so, or the composer has already
// cleared the user's text and it is gone. sendMessage reports false on a failed submit (and true
// once the server admits the message), which is what AgentChatPanel restores its draft from.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { installFakeIndexedDb } from "../../../tests/support/fakeIndexedDb.mjs";
import { installBrowserSurface, session } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

const { freshStorage } = installBrowserSurface();
installFakeIndexedDb();

const parent = session("wf-send");

function controllerWith(submitMessage) {
  freshStorage();
  const controller = new AgentRunController({
    listAgents: async () => ({
      agents: [{ key: "x", label: "X", workflow_type: parent.agent_workflow_type, task_queue: "q", description: "" }]
    }),
    listSessions: async () => [parent],
    agentInterface: async () => [],
    workflowStatus: async (workflow_id) => ({ workflow_id, execution_status: "RUNNING", closed: false }),
    // eslint-disable-next-line require-yield
    async *attach() {},
    submitMessage
  });
  controller.sessions = [parent];
  controller.session = parent;
  return controller;
}

const rejecting = async () => {
  throw new Error("session_id must be at most 1000 bytes");
};

describe("a failed send", () => {
  it("reports false and surfaces the server's message, so the composer keeps the text", async () => {
    const controller = controllerWith(rejecting);
    const sent = await controller.sendMessage({ type: "ask", payload: { text: "keep me" } });
    assert.equal(sent, false);
    assert.equal(controller.connectionError, "session_id must be at most 1000 bytes");
  });

  it("reports true once the server admits the message", async () => {
    const controller = controllerWith(async () => ({
      turn_number: 1,
      turn_id: "t1",
      message_id: "m1",
      accepted_offset: 0,
      disposition: "opened"
    }));
    assert.equal(await controller.sendMessage({ type: "ask", payload: { text: "hi" } }), true);
  });
});
