/**
 * Two readers of one session see one session, and a reader who reloads mid-reply picks up
 * where it left off.
 *
 * The server here is one append-only frame log every attach tails from its `fromOffset`,
 * which is what /api/attach is: two tabs are two controllers attached to the same log, and a
 * reload is a new controller over the same IndexedDB, which survives a reload.
 */
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { installBrowserSurface, session, sleep, waitFor } from "../../../tests/support/controllerHarness.mjs";
import { installFakeIndexedDb } from "../../../tests/support/fakeIndexedDb.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";
import { deleteRuns, readRun, runKeyOf } from "./frameStore.ts";

const { freshStorage } = installBrowserSurface();
installFakeIndexedDb();
const ID = "wf-shared";

/** One exchange: the reader's message, the reply streaming, the reply ending. */
function turn(n) {
  const frame = (offset, event, extra) => ({
    event,
    data: {
      type: event,
      agent_id: "root",
      turn_id: `t${n}`,
      turn_number: n + 1,
      message_id: `m${n}`,
      timestamp: offset,
      event_offset: offset,
      resume_offset: offset + 1,
      replay: false,
      ...extra
    }
  });
  const base = n * 3;
  return [
    frame(base, "message_accepted", { handler: "send_message", payload: { text: `question ${n}` } }),
    frame(base + 1, "reply_delta", { text: `answer ${n}` }),
    frame(base + 2, "message_handler_end", { output: { text: `answer ${n}` } })
  ];
}

function server() {
  const log = [];
  const waiting = new Set();
  return {
    log,
    push(...frames) {
      log.push(...frames);
      for (const wake of waiting) wake();
    },
    async *attach(fromOffset, signal) {
      let next = fromOffset;
      while (!signal?.aborted) {
        while (next < log.length) yield log[next++];
        await new Promise((resolve) => {
          const wake = () => {
            waiting.delete(wake);
            resolve();
          };
          waiting.add(wake);
          signal?.addEventListener("abort", wake, { once: true });
        });
      }
    }
  };
}

function tab(source, attachCalls = []) {
  const controller = new AgentRunController({
    async listSessions() {
      return [session(ID)];
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
    attach(_id, fromOffset, signal) {
      attachCalls.push(fromOffset);
      return source.attach(fromOffset, signal);
    }
  });
  controller.sessions = [session(ID)];
  return controller;
}

const offsets = (controller) => controller.frames.map((frame) => frame.data.event_offset);
const chat = (controller) =>
  controller.chatTranscript.filter((item) => item.kind === "user" || item.kind === "agent").map((item) => item.text);
const expectedChat = (turns) => Array.from({ length: turns }, (_, n) => [`question ${n}`, `answer ${n}`]).flat();

describe("one session, several readers", () => {
  it("delivers the same frames, backlog and live, to two tabs", async () => {
    freshStorage();
    const source = server();
    source.push(...turn(0), ...turn(1));
    const a = tab(source);
    const b = tab(source);
    void a.selectSession(ID);
    void b.selectSession(ID);
    await waitFor("both backlogs", () => a.frames.length === 6 && b.frames.length === 6);

    for (const n of [2, 3, 4]) {
      source.push(...turn(n));
      await sleep(20);
    }
    await waitFor("the live turns in both", () => a.frames.length === 15 && b.frames.length === 15);

    const all = [...source.log.keys()];
    assert.deepEqual(offsets(a), all);
    assert.deepEqual(offsets(b), all);
    assert.deepEqual(chat(a), expectedChat(5));
    assert.deepEqual(chat(b), chat(a));
    a.pause();
    b.pause();
  });

  it("resumes a reload mid-reply from its last offset, duplicating nothing", async () => {
    freshStorage();
    await deleteRuns([runKeyOf(session(ID))]);
    const source = server();
    source.push(...turn(0), ...turn(1), ...turn(2));
    const [accepted, delta, end] = turn(3);
    source.push(accepted, delta);

    const before = tab(source);
    void before.selectSession(ID);
    await waitFor("the stream so far", () => before.frames.length === 11);
    await sleep(900);
    const cached = (await readRun(runKeyOf(session(ID))))?.frames ?? [];
    assert.ok(cached.length > 0, "the reload needs a cached prefix to resume from");
    before.pause();

    /* The run goes on while the page is away. */
    source.push(end, ...turn(4));

    const attachCalls = [];
    const after = tab(source, attachCalls);
    void after.selectSession(ID);
    await waitFor("the reloaded stream", () => after.frames.length === source.log.length);
    await sleep(100);

    assert.equal(attachCalls[0], cached.length, "attach resumes where the cache ends");
    assert.deepEqual(offsets(after), [...source.log.keys()], "every frame exactly once");
    assert.deepEqual(chat(after), expectedChat(5), "every message exactly once");
    after.pause();
  });
});
