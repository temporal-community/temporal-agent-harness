// Tabs following one session share one stream: the tab holding the session's Web Lock attaches
// and relays over a BroadcastChannel, and another takes over when it lets go.

import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";

import { PlainSessionState } from "../src/plainState.ts";
import { AgentSessionCore } from "../src/session.ts";
import { deferred, Frames, ScriptedTransport, until } from "./support.ts";

/** Every open channel in this process, standing in for the other tabs. */
class FakeChannel {
  static readonly open = new Set<FakeChannel>();
  onmessage: ((event: { data: unknown }) => void) | null = null;

  constructor(readonly name: string) {
    FakeChannel.open.add(this);
  }

  postMessage(data: unknown): void {
    for (const other of FakeChannel.open) {
      if (other === this || other.name !== this.name) continue;
      const copy = structuredClone(data);
      queueMicrotask(() => other.onmessage?.({ data: copy }));
    }
  }

  close(): void {
    FakeChannel.open.delete(this);
  }
}

/** Exclusive locks, granted in the order they were asked for. */
class FakeLocks {
  readonly #held = new Set<string>();
  readonly #waiting = new Map<string, Array<() => void>>();

  async request(name: string, options: { signal?: AbortSignal }, callback: () => Promise<unknown>): Promise<unknown> {
    if (this.#held.has(name)) {
      await new Promise<void>((resolve, reject) => {
        const queue = this.#waiting.get(name) ?? [];
        this.#waiting.set(name, [...queue, resolve]);
        options.signal?.addEventListener("abort", () => {
          this.#waiting.set(name, (this.#waiting.get(name) ?? []).filter((r) => r !== resolve));
          reject(new Error("aborted"));
        });
      });
    }
    this.#held.add(name);
    try {
      return await callback();
    } finally {
      this.#held.delete(name);
      this.#waiting.get(name)?.shift()?.();
    }
  }
}

/** Put the fakes where the core looks for Web Locks and BroadcastChannel, until the test ends. */
function stubTabs(t: TestContext, page?: object): void {
  const real = ["BroadcastChannel", "navigator", "document"].map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const);
  Object.defineProperty(globalThis, "BroadcastChannel", { value: FakeChannel, configurable: true, writable: true });
  Object.defineProperty(globalThis, "navigator", { value: { locks: new FakeLocks() }, configurable: true });
  if (page) Object.defineProperty(globalThis, "document", { value: page, configurable: true });
  t.after(() => {
    for (const [name, descriptor] of real) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete (globalThis as Record<string, unknown>)[name];
    }
  });
}

test("one tab attaches for all; another takes over from its last offset when it goes", async (t) => {
  stubTabs(t);

  const f = new Frames();
  const first = f.exchange("m0", "first", 1);
  const second = f.exchange("m1", "second", 2);
  const hold = deferred();
  const tabA = new ScriptedTransport([[...first, { hold: hold.promise }, ...second]]);
  // B's attach after taking over repeats the last frame it already has.
  const tabB = new ScriptedTransport([[second.at(-1)!]]);
  const open = (transport: ScriptedTransport) => {
    const state = new PlainSessionState();
    const core = new AgentSessionCore(
      { sessionId: "s1", transport, idlePollMs: 0, schedule: (flush) => queueMicrotask(flush), shareAcrossTabs: true },
      state
    );
    t.after(() => core.stop());
    return { core, state };
  };

  const a = open(tabA);
  a.core.start();
  await until(() => a.state.messages.length === 1);
  const b = open(tabB);
  b.core.start();
  // A tab that joins late gets what the leader already had.
  await until(() => b.state.messages[0]?.status === "done");

  hold.resolve();
  await until(() => b.state.messages[1]?.status === "done" && a.state.connection === "idle");
  assert.deepEqual(b.state.frames, a.state.frames);
  assert.equal(b.state.connection, "idle");
  assert.deepEqual(tabA.attaches, [0]);
  assert.deepEqual(tabB.attaches, []);

  // A follower sends directly, and wakes the leader to read the reply.
  tabB.submitReply = () => ({ turn_number: 3, turn_id: "turn-3", message_id: "m2", accepted_offset: 12, disposition: "opened" });
  await b.core.sendMessage("ask", { text: "from b" });
  assert.equal(tabB.submitted.length, 1);
  await until(() => tabA.attaches.length === 2);
  assert.deepEqual(tabB.attaches, []);

  a.core.stop();
  await until(() => tabB.attaches.length === 1 && b.state.connection === "idle");
  assert.deepEqual(tabB.attaches, [12]);
  assert.equal(b.state.frames.length, 12);
});

test("a hidden leader still relays frames while its stream stays open", async (t) => {
  stubTabs(t);
  // A hidden tab never gets an animation frame.
  const raf = Object.getOwnPropertyDescriptor(globalThis, "requestAnimationFrame");
  Object.defineProperty(globalThis, "requestAnimationFrame", { value: () => 0, configurable: true, writable: true });
  t.after(() => {
    if (raf) Object.defineProperty(globalThis, "requestAnimationFrame", raf);
    else delete (globalThis as Record<string, unknown>).requestAnimationFrame;
  });

  const f = new Frames();
  const joined = deferred();
  const open = deferred();
  const leader = new ScriptedTransport([[...f.exchange("m0", "first", 1), { hold: joined.promise }, ...f.exchange("m1", "second", 2), { hold: open.promise }]]);
  const start = (transport: ScriptedTransport) => {
    const state = new PlainSessionState();
    const core = new AgentSessionCore({ sessionId: "s4", transport, idlePollMs: 0, shareAcrossTabs: true }, state);
    t.after(() => core.stop());
    core.start();
    return state;
  };

  const a = start(leader);
  await until(() => a.messages.length === 1);
  const b = start(new ScriptedTransport());
  await until(() => b.messages[0]?.status === "done");
  joined.resolve();
  await until(() => b.messages[1]?.status === "done");
  assert.equal(a.connection, "live");
  open.resolve();
});

test("in a page, callbackTools keep each tab on its own stream unless sharing is asked for", async (t) => {
  stubTabs(t, new EventTarget());
  const f = new Frames();
  const open = (shareAcrossTabs?: boolean) => {
    const transport = new ScriptedTransport([f.exchange("m0", "hi", 1)]);
    const core = new AgentSessionCore(
      { sessionId: "s2", transport, idlePollMs: 0, callbackTools: { write_code: () => "" }, shareAcrossTabs },
      new PlainSessionState()
    );
    t.after(() => core.stop());
    core.start();
    return transport;
  };

  // A tab that is not the leader would never answer write_code, so each tab attaches itself.
  const tabs = [open(), open()];
  await until(() => tabs.every((tab) => tab.attaches.length === 1));
  const shared = [open(true), open(true)];
  await until(() => shared[0]!.attaches.length + shared[1]!.attaches.length === 1);
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(shared[0]!.attaches.length + shared[1]!.attaches.length, 1);
});

test("the leader polls while any tab is shown, and a follower being shown polls instead of re-attaching", async (t) => {
  const pages = ["a", "b", "c"].map(() => Object.assign(new EventTarget(), { hidden: true }));
  stubTabs(t, pages[0]);
  const f = new Frames();
  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
  const open = (page: object) => {
    Object.defineProperty(globalThis, "document", { value: page, configurable: true });
    const transport = new ScriptedTransport([f.exchange("m0", "hi", 1)]);
    let asks = 0;
    transport.agentStatus = async () => (asks++, transport.status);
    const state = new PlainSessionState();
    const core = new AgentSessionCore({ sessionId: "s3", transport, idlePollMs: 5, shareAcrossTabs: true }, state);
    t.after(() => core.stop());
    core.start();
    return { core, state, transport, asks: () => asks };
  };
  const show = (page: EventTarget & { hidden: boolean }, shown: boolean) => {
    page.hidden = !shown;
    page.dispatchEvent(new Event("visibilitychange"));
  };

  const a = open(pages[0]!);
  await until(() => a.state.connection === "idle");
  const b = open(pages[1]!);
  const c = open(pages[2]!);
  await until(() => b.state.connection === "idle" && c.state.connection === "idle");
  await sleep(30);
  assert.equal(a.asks(), 0, "no tab is shown, so nobody polls");

  show(pages[1]!, true);
  await until(() => a.asks() >= 2);
  assert.deepEqual(a.transport.attaches, [0], "a follower being shown costs a status poll, not an attach");
  assert.equal(b.asks() + c.asks(), 0, "followers never poll");

  show(pages[1]!, false);
  await sleep(15);
  const settled = a.asks();
  await sleep(30);
  assert.ok(a.asks() <= settled + 1, "with every tab hidden again the leader stops polling");

  // B takes over hidden; it asks who is shown, and C answers.
  show(pages[2]!, true);
  a.core.stop();
  await until(() => b.asks() >= 2);
  assert.equal(c.asks(), 0);
});
