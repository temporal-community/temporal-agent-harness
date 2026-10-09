import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { installBrowserSurface, session, sleep, waitFor } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";
import { maxCachedFrameChars } from "./agentRunStorage.ts";

const { storage } = installBrowserSurface();
const key = (id) => `temporal-agent-ui.frames.v1:${id}`;

/* About 10k characters each, so 300 of them overflow the cache budget. */
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
    delta: `#${offset} ${"x".repeat(10_000)}`,
    replay: true
  }
});
const frames = Array.from({ length: 300 }, (_, i) => frame(i));

function controllerFor(id, attachCalls) {
  const controller = new AgentRunController({
    async listSessions() {
      return [session(id)];
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
    async *attach(_id, fromOffset, signal) {
      attachCalls.push(fromOffset);
      for (const item of frames.slice(fromOffset)) yield item;
      await new Promise((resolve) => signal?.addEventListener("abort", resolve));
    }
  });
  controller.sessions = [session(id)];
  return controller;
}

describe("the frame cache", () => {
  it("keeps a prefix that fits and resumes the stream past it", async () => {
    const first = controllerFor("wf-big", []);
    void first.selectSession("wf-big");
    await waitFor("the backlog", () => first.frames.length === frames.length);
    await sleep(900);

    const raw = storage.session.getItem(key("wf-big"));
    assert.ok(raw, "a session larger than the budget must still cache something");
    assert.ok(raw.length <= maxCachedFrameChars + 100, `cache grew past its budget: ${raw.length}`);
    const cached = JSON.parse(raw).frames;
    assert.ok(cached.length > 0 && cached.length < frames.length, `cached ${cached.length}`);
    assert.deepEqual(cached, frames.slice(0, cached.length), "the cache must be a prefix");
    first.pause();

    const attachCalls = [];
    const reloaded = controllerFor("wf-big", attachCalls);
    void reloaded.selectSession("wf-big");
    await waitFor("the reloaded backlog", () => reloaded.frames.length === frames.length);
    assert.equal(attachCalls[0], cached.length, "attach must resume where the cache ends");
  });

  it("drops other sessions' frames when the quota refuses a write", () => {
    storage.session.clear();
    const quota = 3_000_000;
    const used = () => [...Array(storage.session.length).keys()]
      .map((i) => storage.session.getItem(storage.session.key(i)).length)
      .reduce((a, b) => a + b, 0);
    const setItem = storage.session.setItem;
    storage.session.setItem = (k, v) => {
      const before = storage.session.getItem(k)?.length ?? 0;
      if (used() - before + v.length > quota) throw new Error("QuotaExceededError");
      setItem(k, v);
    };
    storage.session.setItem(key("wf-old"), "x".repeat(1_500_000));

    const controller = controllerFor("wf-new", []);
    return (async () => {
      void controller.selectSession("wf-new");
      await waitFor("the backlog", () => controller.frames.length === frames.length);
      await sleep(900);
      controller.pause();
      storage.session.setItem = setItem;
      assert.equal(storage.session.getItem(key("wf-old")), null, "the stale session should make room");
      assert.ok(storage.session.getItem(key("wf-new")), "and the active session should be cached");
    })();
  });
});
