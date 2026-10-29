// AgentSessionCore against a scripted transport: the connection lifecycle, sends, callbacks.

import assert from "node:assert/strict";
import { mock, test } from "node:test";

import { exposeSession, inspectSession } from "../src/inspect.ts";
import { PlainSessionState } from "../src/plainState.ts";
import type { UntypedAgent } from "../src/schema.ts";
import { AgentSessionCore, type AgentSessionOptions } from "../src/session.ts";
import { deferred, Frames, ScriptedTransport, until } from "./support.ts";

function session(transport: ScriptedTransport, options: Partial<AgentSessionOptions<UntypedAgent>> = {}) {
  const state = new PlainSessionState();
  const core = new AgentSessionCore(
    {
      sessionId: "s1",
      transport,
      reconnectBackoffMs: [1, 1, 1],
      idlePollMs: 0,
      schedule: (flush) => queueMicrotask(flush),
      ...options
    },
    state
  );
  return { core, state };
}

test("it replays the session, then idles until a send wakes it", async (t) => {
  const f = new Frames();
  const history = f.exchange("m0", "earlier", 1);
  const transport = new ScriptedTransport([history]);
  const { core, state } = session(transport);
  t.after(() => core.stop());

  core.start();
  await until(() => state.connection === "idle");
  assert.deepEqual(state.messages.map((m) => [m.id, m.status]), [["m0", "done"]]);
  assert.equal(state.frames.length, history.length);

  transport.submitReply = () => ({ turn_number: 2, turn_id: "turn-2", message_id: "m1", accepted_offset: 6, disposition: "opened" });
  transport.script(f.exchange("m1", "now", 2));
  const reply = await core.sendMessage("ask", { text: "again" });

  assert.equal(reply?.message_id, "m1");
  await until(() => state.messages.length === 2 && state.messages[1]!.status === "done");
  // The re-attach resumed where the stream left off rather than replaying from the start.
  assert.deepEqual(transport.attaches, [0, 6]);
});

test("a stream cut off mid-turn reconnects and does not repeat frames", async (t) => {
  const f = new Frames();
  const [accepted, started, handlerStart, delta, end, turnEnd] = f.exchange("m0", "hi", 1);
  // The second attach overlaps the first: a resume replays frames that share its offset.
  const transport = new ScriptedTransport([
    [accepted!, started!, handlerStart!],
    [handlerStart!, delta!, end!, turnEnd!]
  ]);
  const { core, state } = session(transport);
  t.after(() => core.stop());

  core.start();
  await until(() => state.connection === "idle");
  assert.deepEqual(transport.attaches, [0, 3]);
  assert.equal(state.frames.length, 6);
  assert.deepEqual(state.messages[0]!.parts, [{ type: "reply_delta", text: "hi" }]);
});

test("attach errors are retried until the budget runs out; a send tries again", async (t) => {
  const failure = () => [new Error("connection refused")];
  const transport = new ScriptedTransport([failure(), failure(), failure(), failure()]);
  const { core, state } = session(transport);
  t.after(() => core.stop());

  core.start();
  await until(() => state.connection === "error");
  assert.equal(transport.attaches.length, 4);
  assert.equal(state.error?.message, "connection refused");

  const f = new Frames();
  transport.script(f.exchange("m1", "back", 1));
  await core.sendMessage("ask", { text: "hello?" });
  await until(() => state.connection === "idle");
  assert.equal(state.messages[0]!.status, "done");
});

test("while idle, it polls for work another client gave the agent", async (t) => {
  const f = new Frames();
  const transport = new ScriptedTransport([f.exchange("m0", "one", 1)]);
  const { core, state } = session(transport, { idlePollMs: 2 });
  t.after(() => core.stop());

  core.start();
  await until(() => state.connection === "idle");
  const polledAttaches = transport.attaches.length;

  transport.script(f.exchange("m1", "from another tab", 2));
  transport.status = { current_turn: 2, turn_active: true, pending_turns: [] };
  await until(() => state.messages.length === 2);
  assert.equal(transport.attaches.length, polledAttaches + 1);
});

test("a closed workflow ends the connection", async (t) => {
  const f = new Frames();
  const transport = new ScriptedTransport([f.exchange("m0", "bye", 1)]);
  transport.closed = true;
  const { core, state } = session(transport);
  t.after(() => core.stop());

  core.start();
  await until(() => state.connection === "closed");
  assert.equal(transport.attaches.length, 1);
});

test("stopping aborts a held stream", async () => {
  const f = new Frames();
  const hold = deferred();
  const transport = new ScriptedTransport([[f.make("turn_started"), { hold: hold.promise }]]);
  const { core, state } = session(transport);

  core.start();
  await until(() => state.connection === "live");
  core.stop();
  assert.equal(state.connection, "stopped");
  assert.equal(state.agentStatus, "busy");
});

test("a waiting callback tool is handed to its callbackTools entry once, and its result submitted", async (t) => {
  const f = new Frames();
  const a = { message_id: "m0" };
  const call = { tool_id: "c1", tool_name: "read_local_file" };
  const settled = { tool_id: "c0", tool_name: "read_local_file" };
  const hold = deferred();
  const transport = new ScriptedTransport([
    [
      f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, a),
      f.make("turn_started"),
      // One callback resolved before this client arrived: it must not be fulfilled again.
      f.make("callback_requested", { ...settled, tool_input: { path: "/a" }, output_schema: {} }, a),
      f.make("callback_resolved", { ...settled, outcome: "ok", error: null }, a),
      f.make("callback_requested", { ...call, tool_input: { path: "/b" }, output_schema: { type: "string" } }, a),
      { hold: hold.promise }
    ]
  ]);
  const calls: string[] = [];
  const { core } = session(transport, {
    schedule: () => {},
    callbackTools: {
      read_local_file: (c) => {
        calls.push(`${c.toolId}:${String(c.input.path)}`);
        return "file contents";
      }
    }
  });
  t.after(() => core.stop());

  // Flushing only at the end puts the whole replay in one batch, as a catch-up would be.
  core.start();
  await until(() => transport.attaches.length === 1);
  await new Promise((resolve) => setTimeout(resolve, 5));
  core.stop();
  await until(() => transport.callbackResults.length === 1);
  assert.deepEqual(calls, ["c1:/b"]);
  assert.deepEqual(transport.callbackResults, [{ toolId: "c1", outcome: { result: "file contents" } }]);
});

test("onFinish reports only this client's messages", async (t) => {
  const f = new Frames();
  const transport = new ScriptedTransport([f.exchange("someone-else", "theirs", 1)]);
  const finished: string[] = [];
  const { core, state } = session(transport, { onFinish: (m) => finished.push(m.id) });
  t.after(() => core.stop());

  core.start();
  await until(() => state.connection === "idle");
  transport.submitReply = () => ({ turn_number: 2, turn_id: "turn-2", message_id: "mine", accepted_offset: 6, disposition: "opened" });
  transport.script(f.exchange("mine", "ours", 2));
  await core.sendMessage("ask", { text: "hi" });
  await until(() => finished.length === 1);
  assert.deepEqual(finished, ["mine"]);
});

test("a failed send stays pending with its error, and can be dismissed", async (t) => {
  const transport = new ScriptedTransport();
  transport.submitReply = () => new Error("mid_turn_rejected");
  const { core, state } = session(transport);
  t.after(() => core.stop());

  assert.equal(await core.sendMessage("ask", { text: "hi" }), null);
  assert.deepEqual(state.pending.map((m) => [m.status, m.error]), [["error", "mid_turn_rejected"]]);
  assert.equal(state.error?.message, "mid_turn_rejected");

  core.dismiss(state.pending[0]!.id);
  assert.equal(state.pending.length, 0);
});

test("a 1,500-frame replay costs linear work and one write per flush", async (t) => {
  const f = new Frames();
  const frames = Array.from({ length: 250 }, (_, i) => f.exchange(`m${i}`, `reply ${i}`, i + 1)).flat();
  assert.equal(frames.length, 1_500);
  const transport = new ScriptedTransport([frames]);
  const state = new PlainSessionState();
  let writes = 0;
  const updateAgents = state.updateAgents.bind(state);
  state.updateAgents = (updates) => {
    writes += 1;
    updateAgents(updates);
  };
  const core = new AgentSessionCore(
    { sessionId: "s1", transport, idlePollMs: 0, schedule: () => {} },
    state
  );
  t.after(() => core.stop());

  const started = performance.now();
  core.start();
  await until(() => state.connection === "idle");
  const elapsed = performance.now() - started;

  assert.equal(state.messages.length, 250);
  assert.equal(writes, 1);
  assert.ok(elapsed < 1_000, `took ${elapsed.toFixed(0)}ms`);
});

test("a subagent's approvals and callbacks are answered at the subagent's own workflow", async (t) => {
  const f = new Frames();
  const root = { message_id: "a" };
  const child = { agent_id: "root.s1", message_id: "c1" };
  const sub = { subagent_id: "root.s1", agent_key: "helper", workflow_id: "wf-s1", handler: "ask", subagent_turn: 1 };
  const hold = deferred();
  const transport = new ScriptedTransport([
    [
      f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, root),
      f.make("turn_started"),
      f.make("subagent_message_sent", { ...sub, from_offset: 0 }, root),
      f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, child),
      f.make("tool_approval_requested", { tool_id: "gate", tool_name: "rm", tool_input: {} }, child),
      f.make("callback_requested", { tool_id: "cb", tool_name: "photo", tool_input: {}, output_schema: {} }, child),
      { hold: hold.promise }
    ]
  ]);
  const calls: string[] = [];
  const { core, state } = session(transport, {
    callbackTools: {
      photo: (call) => {
        calls.push(`${call.toolId}@${call.agentId}`);
        return "jpeg";
      }
    }
  });
  t.after(() => core.stop());

  core.start();
  await until(() => state.agents["root.s1"]?.messages.length === 1 && transport.callbackResults.length === 1);
  await core.respondToApproval("gate", { approved: true });

  assert.deepEqual(calls, ["cb@root.s1"]);
  assert.deepEqual(transport.routed, ["wf-s1:cb", "wf-s1:gate"]);
  assert.equal(state.agent("root.s1")!.workflowId, "wf-s1");
});

test("a callback tool with no callbackTools entry is left waiting for someone else", async (t) => {
  const f = new Frames();
  const a = { message_id: "m0" };
  const hold = deferred();
  const transport = new ScriptedTransport([
    [
      f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, a),
      f.make("turn_started"),
      f.make("callback_requested", { tool_id: "c1", tool_name: "read_file", tool_input: {}, output_schema: {} }, a),
      f.make("callback_requested", { tool_id: "c2", tool_name: "confirm_purchase", tool_input: {}, output_schema: {} }, a),
      { hold: hold.promise }
    ]
  ]);
  const { core } = session(transport, { callbackTools: { read_file: () => "contents" } });
  t.after(() => core.stop());

  core.start();
  await until(() => transport.callbackResults.length === 1);
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(transport.callbackResults, [{ toolId: "c1", outcome: { result: "contents" } }]);

  assert.equal(core.handlesCallback("read_file"), true);
  assert.equal(core.handlesCallback("confirm_purchase"), false);
  /* Only the map's own keys: an inherited `constructor` is not a handler. */
  assert.equal(core.handlesCallback("constructor"), false);

  await core.provideToolOutput("c2", { confirmed: true });
  assert.deepEqual(transport.callbackResults.at(-1), { toolId: "c2", outcome: { result: { confirmed: true } } });
});

test("onFlush reports each flush's changes once the state has them", async (t) => {
  const f = new Frames();
  const transport = new ScriptedTransport([[...f.exchange("m0", "earlier", 1)]]);
  // Flushed only when the stream ends or a send settles.
  const { core, state } = session(transport, { schedule: () => {} });
  t.after(() => core.stop());
  const flushes: string[] = [];
  const stop = core.onFlush(({ agents, pending }) => {
    const changed = agents.flatMap(([id, u]) => (u.messages ?? []).map(([i, m]) => `${id}:${i}:${m.id}:${m.status}`));
    flushes.push(`${changed.join(",")}|${pending?.map((m) => m.status).join(",") ?? "-"}`);
    assert.equal(state.messages.length, 1, "the state is written before listeners run");
  });

  core.start();
  await until(() => state.connection === "idle");
  transport.submitReply = () => new Error("down");
  await core.sendMessage("ask", { text: "again" });
  stop();
  core.dismiss("local-1");

  // The replay as one flush, the failed send as another, and nothing after the unsubscribe.
  assert.deepEqual(flushes, ["root:0:m0:done|-", "|error"]);
});

test("a throwing onFlush listener goes to onError and the others still run", async (t) => {
  const f = new Frames();
  const errors: string[] = [];
  const transport = new ScriptedTransport([f.exchange("m0", "hi", 1)]);
  const { core, state } = session(transport, { onError: (error) => errors.push(error.message) });
  t.after(() => core.stop());
  let after = 0;
  core.onFlush(() => {
    throw new Error("listener bug");
  });
  core.onFlush(() => after++);

  core.start();
  await until(() => state.connection === "idle");
  assert.ok(after > 0, "the listener after the throwing one still runs");
  assert.deepEqual(errors, Array(after).fill("listener bug"), "and every throw goes to onError");
  assert.equal(state.error, undefined, "a listener's bug is not the session's error");
});

test("an exposed session is inspected as plain data from the global hook", async (t) => {
  const f = new Frames();
  const transport = new ScriptedTransport([
    [
      ...f.exchange("m0", "earlier", 1),
      f.make("tool_approval_requested", { tool_id: "gate", tool_name: "rm", tool_input: {} }, { message_id: "m0" })
    ]
  ]);
  const { core, state } = session(transport);
  t.after(() => core.stop());
  const unexpose = exposeSession("s1", state);
  core.start();
  await until(() => state.connection === "idle");

  const hook = (globalThis as { __AGENT_HARNESS__?: Record<string, () => unknown> }).__AGENT_HARNESS__!;
  const seen = hook.s1!();
  assert.deepEqual(JSON.parse(JSON.stringify(seen)), seen);
  assert.deepEqual(seen, {
    connection: "idle",
    error: null,
    rootAgentId: "root",
    lastOffset: 7,
    frames: 7,
    pendingSends: 0,
    waitingApprovals: 1,
    waitingCallbacks: 0,
    agents: {
      root: { agentId: "root", parentId: null, agentKey: null, workflowId: null, lifecycle: "running", unavailable: null, agentStatus: "idle", messages: 1, states: [] }
    }
  });
  unexpose();
  assert.equal(hook.s1, undefined);
});

test("the idle poll asks every 30s by default, and not at all while the page is hidden", async (t) => {
  const page = Object.assign(new EventTarget(), { hidden: true });
  Object.defineProperty(globalThis, "document", { value: page, configurable: true });
  mock.timers.enable({ apis: ["setTimeout"] });
  const transport = new ScriptedTransport([[]]);
  let asks = 0;
  transport.agentStatus = async () => (asks++, transport.status);
  const { core, state } = session(transport, { idlePollMs: undefined });
  t.after(() => {
    core.stop();
    mock.timers.reset();
    delete (globalThis as { document?: unknown }).document;
  });
  const settle = async () => {
    for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
  };

  core.start();
  await settle();
  assert.equal(state.connection, "idle");
  mock.timers.tick(120_000);
  await settle();
  assert.equal(asks, 0, "a hidden page does not poll");

  page.hidden = false;
  page.dispatchEvent(new Event("visibilitychange"));
  await settle();
  assert.equal(asks, 1, "showing the page polls at once");
  mock.timers.tick(29_999);
  await settle();
  assert.equal(asks, 1);
  mock.timers.tick(1);
  await settle();
  assert.equal(asks, 2);
});

test("inspecting a core counts what it asked for, and the Actions that cost", async (t) => {
  const f = new Frames();
  const transport = new ScriptedTransport([f.exchange("m0", "hi", 1)]);
  const { core, state } = session(transport);
  t.after(() => core.stop());
  core.start();
  await until(() => state.connection === "idle");
  await core.sendMessage("ask", { text: "again" });
  await core.respondToApproval("gate", { approved: true });
  await until(() => state.connection === "idle" && transport.attaches.length >= 2);

  const attaches = transport.attaches.length;
  assert.deepEqual(inspectSession(core).activity, {
    attaches,
    statusQueries: 0,
    sends: 1,
    approvals: 1,
    callbackResults: 0,
    closes: 0,
    tab: "solo",
    framesPerSecond: 0.6,
    estimatedActions: 3 * attaches + 2
  });
  assert.equal(inspectSession(state).activity, undefined);
});


test("the frame rate counts only the last 10s however many frames came before", async (t) => {
  mock.timers.enable({ apis: ["Date"], now: 0 });
  const f = new Frames();
  const burst = Array.from({ length: 2_000 }, () => f.make("reply_delta", { text: "x" }, { message_id: "m0" }));
  const transport = new ScriptedTransport([burst]);
  const { core, state } = session(transport);
  t.after(() => {
    core.stop();
    mock.timers.reset();
  });
  core.start();
  await until(() => state.connection === "idle");
  assert.equal(inspectSession(core).activity?.framesPerSecond, 200);

  mock.timers.tick(11_000);
  transport.script([f.make("reply_delta", { text: "y" }, { message_id: "m0" })]);
  await core.sendMessage("ask", { text: "again" });
  await until(() => state.frames.length === 2_001);
  assert.equal(inspectSession(core).activity?.framesPerSecond, 0.1);
});
