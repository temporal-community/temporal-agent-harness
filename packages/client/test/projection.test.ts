// SessionProjection: frames folded into messages and state.

import assert from "node:assert/strict";
import { test } from "node:test";

import type { HarnessMessage, ToolPart } from "../src/messages.ts";
import { SessionProjection } from "../src/projection.ts";
import { Frames } from "./support.ts";

function project(frames: ReturnType<Frames["make"]>[]): SessionProjection {
  const projection = new SessionProjection();
  for (const frame of frames) projection.apply(frame);
  return projection;
}

function tool(message: HarnessMessage | undefined): ToolPart {
  const part = message?.parts.find((p) => p.type === "tool");
  assert.ok(part && part.type === "tool", "the message has a tool part");
  return part;
}

test("two messages streaming in one turn keep their own replies", () => {
  const f = new Frames();
  const a = { message_id: "a" };
  const b = { message_id: "b" };
  const p = project([
    f.make("message_accepted", { handler: "ask", payload: { text: "one" }, disposition: "opened" }, a),
    f.make("turn_started"),
    f.make("message_handler_start", {}, a),
    f.make("message_accepted", { handler: "note", payload: { text: "two" }, disposition: "joined" }, b),
    f.make("message_handler_start", {}, b),
    f.make("reply_delta", { text: "Hel" }, a),
    f.make("reply_delta", { text: "Other" }, b),
    f.make("reply_delta", { text: "lo" }, a),
    f.make("message_handler_end", { output: { text: "Other" } }, b),
    f.make("message_handler_end", { output: { text: "Hello" } }, a),
    f.make("turn_end")
  ]);

  assert.deepEqual(
    p.root!.messages.map((m) => [m.id, m.handler, m.status, m.disposition, m.parts]),
    [
      ["a", "ask", "done", "opened", [{ type: "reply_delta", text: "Hello" }]],
      ["b", "note", "done", "joined", [{ type: "reply_delta", text: "Other" }]]
    ]
  );
  assert.deepEqual(p.root!.messages[0]!.output, { text: "Hello" });
  assert.equal(p.root!.agentStatus, "idle");
});

test("a queued message stays accepted until its handler starts", () => {
  const f = new Frames();
  const q = { message_id: "q", turn_number: 2 };
  const p = project([
    ...f.exchange("first", "done", 1).slice(0, 3),
    f.make("message_accepted", { handler: "ask", payload: {}, disposition: "queued" }, q)
  ]);
  const queued = p.root!.messages[1]!;
  assert.equal(queued.status, "accepted");
  assert.equal(queued.disposition, "queued");
  assert.equal(queued.turnNumber, 2);

  p.apply(f.make("message_handler_start", {}, q));
  assert.equal(p.root!.messages[1]!.status, "running");
});

test("an approval resolved with no message id lands on the older message's tool", () => {
  const f = new Frames();
  const a = { message_id: "a" };
  const b = { message_id: "b" };
  const call = { tool_id: "t1", tool_name: "delete_file" };
  const p = project([
    f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, a),
    f.make("tool_requested", { ...call, tool_input: { path: "/x" } }, a),
    f.make("tool_approval_requested", { ...call, tool_input: { path: "/x" } }, a),
    f.make("message_accepted", { handler: "ask", payload: {}, disposition: "joined" }, b),
    f.make("reply_delta", { text: "meanwhile" }, b)
  ]);
  assert.equal(tool(p.root!.messages[0]).state, "awaiting_approval");

  // A policy cascade resolves it from an update handler, bound to no message.
  p.apply(f.make("tool_approval_resolved", { ...call, approved: true, reason: "allowed", remember: true }));
  p.apply(f.make("tool_start", { ...call, tool_input: { path: "/x" } }, a));

  const part = tool(p.root!.messages[0]);
  assert.equal(part.state, "running");
  assert.deepEqual(part.approval, { approved: true, reason: "allowed", remember: true });
  assert.deepEqual(p.root!.messages[1]!.parts, [{ type: "reply_delta", text: "meanwhile" }]);
});

test("an evaluator that escalates leaves the gate with a person", () => {
  const f = new Frames();
  const a = { message_id: "a" };
  const call = { tool_id: "t1", tool_name: "refund", evaluator: "jev_evaluator" };
  const p = project([
    f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, a),
    f.make("tool_approval_requested", { tool_id: "t1", tool_name: "refund", tool_input: {} }, a),
    f.make("auto_approval_evaluation_started", { ...call, evaluation_id: "e1" }, a)
  ]);
  assert.equal(tool(p.root!.messages[0]).state, "evaluating");

  p.apply(
    f.make(
      "auto_approval_evaluation_ended",
      { ...call, evaluation_id: "e1", verdict: "escalate", reason: "large amount", details: { confidence: 0.4 } },
      a
    )
  );
  const part = tool(p.root!.messages[0]);
  assert.equal(part.state, "awaiting_approval");
  assert.deepEqual(part.evaluations.map((e) => [e.status, e.verdict, e.reason]), [
    ["ended", "escalate", "large amount"]
  ]);
});

test("a tool that starts with no request is still tracked", () => {
  const f = new Frames();
  const a = { message_id: "a" };
  const p = project([
    f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, a),
    f.make("tool_start", { tool_id: "search-1", tool_name: "file_search", tool_input: { q: "x" } }, a),
    f.make("tool_end", { tool_id: "search-1", tool_name: "file_search", tool_output: "3 hits" }, a)
  ]);
  const part = tool(p.root!.messages[0]);
  assert.deepEqual([part.state, part.input, part.output], ["done", { q: "x" }, "3 hits"]);
});

test("steps bracket model calls, and reasoning accumulates", () => {
  const f = new Frames();
  const a = { message_id: "a" };
  const p = project([
    f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, a),
    f.make("model_interaction_started", { model: "m" }, a),
    f.make("thought_summary", { delta: { content: { type: "text", text: "Let me " } } }, a),
    f.make("thought_summary", { delta: { delta: "think." } }, a),
    f.make("reply_delta", { text: "Hi" }, a),
    f.make("model_interaction_ended", { model: "m", usage: { input_tokens: 3 } }, a)
  ]);
  assert.deepEqual(p.root!.messages[0]!.parts, [
    { type: "model_interaction", model: "m", status: "done", usage: { input_tokens: 3 } },
    { type: "thought_summary", text: "Let me think." },
    { type: "reply_delta", text: "Hi" }
  ]);
});

test("state follows its snapshot and patches, and stops at a gap", () => {
  const f = new Frames();
  const p = project([
    f.make("state_snapshot", { state_id: "board", version: 0, value: { cells: [null, null] } }, { turn_number: 0 }),
    f.make("state_patch", { state_id: "board", version: 1, ops: [{ op: "replace", path: "/cells/0", value: "X" }] }),
    // A redelivery of version 1 is ignored.
    f.make("state_patch", { state_id: "board", version: 1, ops: [{ op: "replace", path: "/cells/0", value: "O" }] })
  ]);
  assert.deepEqual(p.root!.states.get("board"), { value: { cells: ["X", null] }, version: 1, error: null });

  p.apply(f.make("state_patch", { state_id: "board", version: 3, ops: [] }));
  const board = p.root!.states.get("board")!;
  assert.deepEqual(board.value, { cells: ["X", null] });
  assert.match(board.error!, /versions in between are missing/);
});

test("every subagent in the merged stream gets its own view, linked from the parent's part", () => {
  const f = new Frames();
  const root = { message_id: "a" };
  const child = { agent_id: "root.s1", message_id: "c1", turn_number: 1 };
  const grandchild = { agent_id: "root.s1.g1", message_id: "g1", turn_number: 1 };
  const sub = { subagent_id: "root.s1", agent_key: "researcher", workflow_id: "wf-s1", handler: "ask", subagent_turn: 1 };
  const subsub = { subagent_id: "root.s1.g1", agent_key: "fetcher", workflow_id: "wf-g1", handler: "fetch", subagent_turn: 1 };
  const p = project([
    f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, root),
    f.make("subagent_started", { subagent_id: "root.s1", agent_key: "researcher", workflow_id: "wf-s1" }, root),
    f.make("subagent_message_sent", { ...sub, from_offset: 0 }, root),
    // The child's own turn, merged in after the parent asked for it.
    f.make("message_accepted", { handler: "ask", payload: { text: "look it up" }, disposition: "opened" }, child),
    f.make("turn_started", {}, child),
    f.make("reply_delta", { text: "found it" }, child),
    // ...which itself drives a grandchild.
    f.make("subagent_message_sent", { ...subsub, from_offset: 0 }, child),
    f.make("message_accepted", { handler: "fetch", payload: {}, disposition: "opened" }, grandchild),
    f.make("message_handler_end", { output: { text: "page" } }, grandchild),
    f.make("subagent_reply_received", { ...subsub, outcome: "ok" }, child),
    f.make("message_handler_end", { output: { text: "found it" } }, child),
    f.make("turn_end", {}, child),
    f.make("subagent_reply_received", { ...sub, outcome: "ok" }, root)
  ]);

  assert.deepEqual([...p.agents.keys()], ["root", "root.s1", "root.s1.g1"]);
  const s1 = p.agents.get("root.s1")!;
  assert.deepEqual(s1.info, {
    agentId: "root.s1",
    parentId: "root",
    agentKey: "researcher",
    workflowId: "wf-s1",
    lifecycle: "running",
    unavailable: null
  });
  assert.deepEqual(s1.messages.map((m) => [m.id, m.status, m.parts.map((part) => part.type)]), [
    ["c1", "done", ["reply_delta", "subagent"]]
  ]);
  assert.equal(p.agents.get("root.s1.g1")!.info.parentId, "root.s1");

  // The parent's part names the child's message, so a view can render it in place.
  assert.deepEqual(p.root!.messages[0]!.parts, [
    {
      type: "subagent",
      subagentId: "root.s1",
      agentKey: "researcher",
      workflowId: "wf-s1",
      handler: "ask",
      subagentTurn: 1,
      status: "ok",
      messageId: "c1"
    }
  ]);
  assert.equal(p.root!.messages.length, 1, "the child's messages are not the root's");
});

test("a stopped subagent, and one whose stream was lost, say so", () => {
  const f = new Frames();
  const root = { message_id: "a" };
  const sub = { subagent_id: "root.s1", agent_key: "helper", workflow_id: "wf-s1", handler: "ask", subagent_turn: 1 };
  const p = project([
    f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, root),
    f.make("subagent_message_sent", { ...sub, from_offset: 0 }, root),
    f.make("subagent_stream_unavailable", { subagent_id: "root.s1", workflow_id: "wf-s1", reason: "completed" }, { agent_id: "root.s1" })
  ]);
  const part = p.root!.messages[0]!.parts[0];
  assert.equal(part?.type === "subagent" ? part.status : null, "unavailable");
  assert.equal(p.agents.get("root.s1")!.info.unavailable, "completed");

  // The parent's own record still closes the turn.
  p.apply(f.make("subagent_reply_received", { ...sub, outcome: "ok" }, root));
  p.apply(f.make("subagent_stopped", { subagent_id: "root.s1", agent_key: "helper", workflow_id: "wf-s1" }, root));
  const closed = p.root!.messages[0]!.parts[0];
  assert.equal(closed?.type === "subagent" ? closed.status : null, "ok");
  assert.equal(p.agents.get("root.s1")!.info.lifecycle, "stopped");
});

test("published messages never change afterwards", () => {
  const f = new Frames();
  const a = { message_id: "a" };
  const p = project([
    f.make("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, a),
    f.make("reply_delta", { text: "one" }, a)
  ]);
  const [[, first]] = p.takeChanges().agents[0]![1].messages as [[number, HarnessMessage]];
  const firstPart = first.parts[0];

  p.apply(f.make("reply_delta", { text: " two" }, a));
  const [[, second]] = p.takeChanges().agents[0]![1].messages as [[number, HarnessMessage]];

  assert.deepEqual(first.parts, [{ type: "reply_delta", text: "one" }]);
  assert.equal(first.parts[0], firstPart);
  assert.deepEqual(second.parts, [{ type: "reply_delta", text: "one two" }]);
  assert.notEqual(second, first);
});

test("a sent message waits in pending until the server admits it", () => {
  const f = new Frames();
  const p = new SessionProjection();
  const sending = {
    id: "local-1", handler: "ask", input: { text: "hi" }, output: null, status: "sending" as const,
    disposition: null, turnNumber: null, parts: [], error: null, acceptedAt: null, startedAt: null, endedAt: null
  };

  p.addSending(sending);
  assert.deepEqual(p.pending.map((m) => m.id), ["local-1"]);
  assert.equal(p.root, undefined);

  // The stream admits it before the submit returns...
  p.apply(f.make("message_accepted", { handler: "ask", payload: { text: "hi" }, disposition: "opened" }, { message_id: "m1" }));
  p.admitSending("local-1", "m1");
  assert.deepEqual([p.pending.length, p.root!.messages.map((m) => m.id)], [0, ["m1"]]);

  // ...or after, in which case the message joins now and its event fills it in.
  p.addSending({ ...sending, id: "local-2" });
  p.admitSending("local-2", "m2");
  assert.deepEqual(p.root!.messages.map((m) => [m.id, m.status, m.disposition]), [
    ["m1", "accepted", "opened"],
    ["m2", "accepted", null]
  ]);
  p.apply(f.make("message_accepted", { handler: "ask", payload: { text: "hi" }, disposition: "queued" }, { message_id: "m2", turn_number: 2 }));
  assert.deepEqual([p.root!.messages[1]!.disposition, p.root!.messages[1]!.turnNumber], ["queued", 2]);
});
