import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { installFakeIndexedDb } from "../../../tests/support/fakeIndexedDb.mjs";
import { installBrowserSurface, session, waitFor } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";
import { appendRunFrames, runKeyOf } from "./frameStore.ts";

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
    delta: `#${offset} `,
    replay: true
  }
});

/* The sessionStorage cache this replaced replayed in slices that waited for a paint, so a
   reload in a background tab never finished. */
describe("restoring a stored run", () => {
  it("finishes in a hidden tab, where requestAnimationFrame never fires, and resumes past it", async () => {
    installBrowserSurface({ controllableRaf: true });
    installFakeIndexedDb();
    const frames = Array.from({ length: 2_000 }, (_, i) => frame(i));
    const stored = session("wf-hidden");
    await appendRunFrames(
      {
        runKey: runKeyOf(stored),
        workflowId: stored.workflow_id,
        lastResumeOffset: frames.length,
        offsets: {},
        count: frames.length,
        lastAccess: Date.now()
      },
      0,
      frames
    );
    const attachedFrom = [];
    const controller = new AgentRunController({
      async workflowStatus(workflowId) {
        return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
      },
      async agentInterface() {
        return [];
      },
      async *attach(_id, from, signal) {
        attachedFrom.push(from);
        await new Promise((resolve) => signal?.addEventListener("abort", resolve));
      }
    });
    controller.sessions = [stored];
    void controller.selectSession("wf-hidden");
    await waitFor("the stored frames", () => controller.frames.length === frames.length, 3_000);
    await waitFor("the stream", () => attachedFrom.length === 1);
    assert.equal(attachedFrom[0], frames.length, "the stream resumes past what was restored");
    controller.pause();
  });
});
