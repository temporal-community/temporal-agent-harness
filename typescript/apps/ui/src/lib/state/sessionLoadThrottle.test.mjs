import assert from "node:assert/strict";
import { beforeAll, describe, it } from "vitest";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

describe("session list loads", () => {
  beforeAll(() => installBrowserSurface());

  it("collapse a burst of unrequested loads but never the refresh button", async () => {
    let calls = 0;
    const controller = new AgentRunController({
      async listSessions() {
        calls += 1;
        await new Promise((resolve) => setTimeout(resolve, 20));
        return [];
      }
    });

    for (let i = 0; i < 10; i += 1) {
      await (i % 2 ? controller.ensureSessionsEnriched() : controller.syncSessions());
    }
    assert.equal(calls, 1, "ten focus/picker loads inside one window should list once");

    await controller.refreshSessions();
    assert.equal(calls, 2, "the refresh button must still reach the server");
  });
});
