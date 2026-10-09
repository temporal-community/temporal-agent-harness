import { describe, it } from "vitest";

import { installBrowserSurface, session, waitFor } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

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

describe("hydrating the frame cache", () => {
  it("finishes in a hidden tab, where requestAnimationFrame never fires", async () => {
    const { storage } = installBrowserSurface({ controllableRaf: true });
    const frames = Array.from({ length: 2_000 }, (_, i) => frame(i));
    storage.session.setItem("temporal-agent-ui.frames.v1:wf-hidden", JSON.stringify({ frames }));
    const controller = new AgentRunController({
      async workflowStatus(workflowId) {
        return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
      },
      async agentInterface() {
        return [];
      },
      async operatorInterface() {
        return [];
      },
      async *attach(_id, _from, signal) {
        await new Promise((resolve) => signal?.addEventListener("abort", resolve));
      }
    });
    controller.sessions = [session("wf-hidden")];
    void controller.selectSession("wf-hidden");
    await waitFor("the cached frames", () => controller.frames.length === frames.length, 3_000);
    controller.pause();
  });
});
