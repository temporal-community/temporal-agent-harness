// A failed session create must not break the session that was open. startNewSession used to stop
// the open session's stream before the POST and, on failure, put the create error in the open
// session's connectionError and never reattach: its chat showed the create error, the top bar read
// "Needs attention", and the session stopped updating until it was selected again.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { installBrowserSurface, waitFor } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

const { freshStorage } = installBrowserSurface();

const frame = (offset) => ({
  event: "reply_delta",
  data: {
    type: "reply_delta",
    agent_id: "root",
    turn_id: "t1",
    turn_number: 1,
    message_id: "m1",
    timestamp: offset,
    resume_offset: offset + 1,
    event_offset: offset,
    delta: `#${offset} `
  }
});

describe("a failed session create", () => {
  it("leaves the open session attached, streaming, and without an error", async () => {
    freshStorage();
    let release;
    const later = new Promise((resolve) => (release = resolve));
    let signal;
    const agents = [{ key: "qa", label: "QA", workflow_type: "X", task_queue: "q", description: "" }];
    const api = {
      listAgents: async () => ({ agents }),
      acceptedMessageTypes: async () => ({ accepted: [] }),
      listSessions: async () => [],
      agentInterface: async () => [],
      operatorInterface: async () => [],
      workflowStatus: async (workflow_id) => ({ workflow_id, execution_status: "RUNNING", closed: false }),
      createSession: async () => {
        throw new Error("500 Internal Server Error");
      },
      async *attach(_sessionId, _fromOffset, attachSignal) {
        signal = attachSignal;
        yield frame(0);
        await later;
        yield frame(1);
        await new Promise((resolve) => attachSignal.addEventListener("abort", resolve, { once: true }));
      }
    };
    const controller = new AgentRunController(api);
    controller.agents = agents;
    controller.sessions = [{ workflow_id: "wf-open", created_at: 1, label: "open", agent_workflow_type: "X" }];

    void controller.selectSession("wf-open");
    await waitFor("the open session to stream", () => controller.frames.length === 1);

    await controller.startNewSession("X");

    assert.equal(controller.createError, "500 Internal Server Error", "the setup dialog is told why");
    assert.equal(controller.connectionError, null, "the open session is not");
    assert.equal(controller.session?.workflow_id, "wf-open");
    assert.equal(signal.aborted, false, "its stream was never stopped");
    assert.equal(controller.creatingSession, false);

    release();
    await waitFor("the open session to keep streaming", () => controller.frames.length === 2);
  });
});
