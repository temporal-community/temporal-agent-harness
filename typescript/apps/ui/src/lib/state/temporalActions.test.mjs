/**
 * Every /api/attach costs two Queries and an Update per long-poll batch, and
 * /api/agent-interface a Query, against each workflow it names. These pin the
 * calls the console used to make for nothing.
 */
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { installFakeIndexedDb } from "../../../tests/support/fakeIndexedDb.mjs";
import { installBrowserSurface, session, sleep, waitFor } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";
import { writeStoredActiveSessionId } from "./agentRunStorage.ts";
import { appendRunFrames, runKeyOf } from "./frameStore.ts";

const { freshStorage } = installBrowserSurface();
installFakeIndexedDb();

/** Each case gets its own parent, so a run another case stored is not restored into it. */
let parent = session("wf-parent");
let cases = 0;
let offset = 0;
const rootFrame = (event, data = {}) => {
  offset += 1;
  return {
    event,
    data: {
      type: event,
      agent_id: "root",
      turn_id: "t1",
      turn_number: 1,
      timestamp: offset,
      resume_offset: offset,
      event_offset: offset,
      ...data
    }
  };
};
const subagentStarted = (workflowId, subagentId) =>
  rootFrame("subagent_started", { workflow_id: workflowId, subagent_id: subagentId, agent_key: "qa" });
const subagentStopped = (workflowId, subagentId) =>
  rootFrame("subagent_stopped", { workflow_id: workflowId, subagent_id: subagentId });
const messageSent = (workflowId, subagentId, turn) =>
  rootFrame("subagent_message_sent", {
    workflow_id: workflowId,
    subagent_id: subagentId,
    subagent_turn: turn,
    replay: false
  });

/**
 * A controller over a recording API. `streamFor(workflowId, call)` yields an
 * attach's frames; the stream then stays open unless it returns `"end"`.
 */
function boot({ streamFor = () => [], reply = {}, interfaceFails = () => false } = {}) {
  freshStorage();
  writeStoredActiveSessionId(parent.workflow_id);
  const calls = { attach: [], agentInterface: [] };
  const api = {
    async listAgents() {
      return {
        agents: [{ key: "qa", label: "QA", workflow_type: parent.agent_workflow_type, task_queue: "q", description: "" }]
      };
    },
    async listSessions() {
      return [parent];
    },
    async workflowStatus(workflowId) {
      return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
    },
    async agentInterface(workflowId) {
      calls.agentInterface.push(workflowId);
      if (interfaceFails(workflowId)) throw new Error("no worker");
      return [{ name: "chat", parameters: {} }];
    },
    async submitMessage({ session_id }) {
      return {
        turn_number: 1,
        turn_id: "t1",
        message_id: `m-${session_id}`,
        accepted_offset: 0,
        disposition: "opened",
        ...reply
      };
    },
    async *attach(workflowId, fromOffset, signal) {
      calls.attach.push({ workflowId, fromOffset });
      const frames = streamFor(workflowId, calls.attach.filter((c) => c.workflowId === workflowId).length);
      const items = Array.isArray(frames) ? frames : frames.items;
      for (const frame of items) yield frame;
      if (!Array.isArray(frames) && frames.end) return;
      await new Promise((resolve) => signal?.addEventListener("abort", resolve));
    }
  };
  return { controller: new AgentRunController(api), calls };
}

async function storeRun(frames, offsets = {}) {
  cases += 1;
  parent = session(`wf-parent-${cases}`);
  await appendRunFrames(
    {
      runKey: runKeyOf(parent),
      workflowId: parent.workflow_id,
      lastResumeOffset: frames.at(-1)?.data.resume_offset ?? 0,
      offsets,
      count: frames.length,
      lastAccess: Date.now()
    },
    0,
    frames
  );
}

describe("Temporal actions the console spends", () => {
  it("resumes a subagent's own stream from its stored offset, not from 0", async () => {
    await storeRun([subagentStarted("wf-kid", "kid")], { "wf-kid": 7 });
    const { controller, calls } = boot();
    void controller.initialize();
    await waitFor("the parent's attach", () => calls.attach.length === 1);
    await controller.sendMessage({ text: "hi" }, "wf-kid");
    await waitFor("the subagent's attach", () => calls.attach.some((c) => c.workflowId === "wf-kid"));
    assert.deepEqual(
      calls.attach.find((c) => c.workflowId === "wf-kid"),
      { workflowId: "wf-kid", fromOffset: 7 },
      "attach(0) replays the child's whole history, a batch of Queries and an Update at a time"
    );
    controller.pause();
  });

  it("asks each workflow for its interface once, and never a child that is already stopped", async () => {
    await storeRun([
      subagentStarted("wf-done", "done"),
      subagentStopped("wf-done", "done"),
      subagentStarted("wf-down", "down"),
      messageSent("wf-down", "down", 1)
    ]);
    const { controller, calls } = boot({
      interfaceFails: (id) => id === "wf-down",
      streamFor: (id) => (id === parent.workflow_id ? [messageSent("wf-down", "down", 2)] : [])
    });
    void controller.initialize();
    await waitFor("the parent's live frame", () => controller.frames.length === 5);
    await sleep(20);
    assert.deepEqual(
      calls.agentInterface.sort(),
      ["wf-down", parent.workflow_id],
      "a failed lookup is not repeated per frame, and a stopped child has no composer to fill"
    );

    await controller.retryAgentInterface("wf-down");
    assert.equal(calls.agentInterface.length, 3, "the retry control still asks again");
    controller.pause();
  });

  it("leaves a joined subagent message to the parent stream that brackets its turn", async () => {
    await storeRun([]);
    const { controller, calls } = boot({
      reply: { disposition: "joined", turn_number: 3 },
      streamFor: (id) => (id === parent.workflow_id ? [messageSent("wf-kid", "kid", 3)] : [])
    });
    void controller.initialize();
    await waitFor("the parent's bracket", () => controller.frames.length === 1);

    await controller.sendMessage({ text: "and also" }, "wf-kid");
    await sleep(20);
    assert.deepEqual(
      calls.attach.map((c) => c.workflowId),
      [parent.workflow_id],
      "the merge already carries turn 3 of the child; a second attach doubles its Queries and Updates"
    );

    controller.pause();
  });

  it("still attaches a subagent for a turn the parent never bracketed", async () => {
    await storeRun([]);
    const { controller, calls } = boot({
      reply: { disposition: "opened", turn_number: 4 },
      streamFor: (id) => (id === parent.workflow_id ? [messageSent("wf-kid", "kid", 3)] : [])
    });
    void controller.initialize();
    await waitFor("the parent's bracket", () => controller.frames.length === 1);

    await controller.sendMessage({ text: "a turn of its own" }, "wf-kid");
    await waitFor("the subagent's attach", () => calls.attach.some((c) => c.workflowId === "wf-kid"));
    controller.pause();
  });
});
