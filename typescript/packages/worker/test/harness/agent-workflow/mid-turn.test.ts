// Mid-turn admission: a message that arrives while a turn is open is rejected, queued or joins the
// open turn, as its handler's mid_turn says.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import type { WorkflowHandle } from "@temporalio/client";
import type { AgentEvent } from "@temporalio/agent-harness/harness/agent-protocol";
import { JoinedTurnError } from "../../../src/harness/agent-client.ts";
import { assertMatch } from "../../match.ts";
import { collect, eventually, send, startEnv, type TestEnv } from "../../support.ts";
import { agentClient, rejection, status, updateFailure } from "./helpers.ts";

let env: TestEnv;
before(async () => {
  env = await startEnv("mid-turn");
});
after(async () => {
  await env.stop();
});

describe("mid-turn admission", () => {
  async function holdATurnOpen() {
    const h = await env.start("MidTurnProbeAgent");
    const work = await send(h, "work", { text: "long" });
    await eventually(async () => (await status(h)).turn_active);
    return { h, work };
  }
  const release = (h: WorkflowHandle, which: string) => h.signal("release", which);
  const ours = (events: AgentEvent[], turnId: string) => events.filter((e) => e.turn_id === turnId);
  const count = <T>(items: T[], item: T) => items.filter((x) => x === item).length;

  it("a reject handler fails while a turn is open and is admitted once idle", async () => {
    const { h } = await holdATurnOpen();
    const cause = await updateFailure(send(h, "exclusive", { text: "now" }));
    assert.equal(cause.type, "MidTurnRejected");
    assert.match(cause.message, /exclusive/);
    assertMatch(await status(h), { pending_turns: [], current_turn: 1 });
    await release(h, "work");
    await eventually(async () => !(await status(h)).turn_active);
    assert.equal((await send(h, "exclusive", { text: "later" })).turn_number, 2);
  });

  it("an accept handler joins the open turn, with one turn_started/turn_end pair", async () => {
    const { h, work } = await holdATurnOpen();
    const steer = await send(h, "steer", { text: "ride along" });
    assertMatch(steer, {
      turn_id: work.turn_id,
      turn_number: work.turn_number,
      disposition: "joined",
    });
    assertMatch(await status(h), { turn_active: true, pending_turns: [] });
    assert.equal((await send(h, "steer", { text: "and again" })).turn_id, work.turn_id);
    assertMatch(await status(h), { current_turn: work.turn_number, pending_turns: [] });
    await release(h, "work");
    const types = ours(await collect(env.client, h.workflowId), work.turn_id).map(
      (e) => e.event.type,
    );
    assert.equal(count(types, "turn_started"), 1);
    assert.equal(count(types, "turn_end"), 1);
    assert.equal(types.at(-1), "turn_end");
    assert.equal(count(types, "message_handler_end"), 3);
  });

  it("a shared turn pairs each reply with its own message; brackets carry no message id", async () => {
    const { h, work } = await holdATurnOpen();
    const joined = await send(h, "outlive", { text: "after you" });
    assert.equal(joined.turn_id, work.turn_id);
    assert.notEqual(joined.message_id, work.message_id);
    await release(h, "outlive");
    await release(h, "work");
    const events = ours(await collect(env.client, h.workflowId), work.turn_id);
    const replies = Object.fromEntries(
      events.flatMap((e) =>
        e.event.type === "message_handler_end" && "text" in e.event.output
          ? [[e.message_id, e.event.output.text as string]]
          : [],
      ),
    );
    assert.match(replies[work.message_id]!, /^worked:/);
    assert.match(replies[joined.message_id]!, /^outlived:/);
    const brackets = events.filter(
      (e) => e.event.type === "turn_started" || e.event.type === "turn_end",
    );
    assert.deepStrictEqual(
      brackets.map((e) => e.message_id),
      [null, null],
    );
    assert.deepStrictEqual(
      new Set(events.filter((e) => !brackets.includes(e)).map((e) => e.message_id)),
      new Set([work.message_id, joined.message_id]),
    );
  });

  it("message_accepted reports the disposition: opened, queued, joined", async () => {
    const { h, work } = await holdATurnOpen();
    const queued = await send(h, "work", { text: "next" });
    const joined = await send(h, "steer", { text: "ride along" });
    assert.deepStrictEqual(
      [work.disposition, queued.disposition, joined.disposition],
      ["opened", "queued", "joined"],
    );
    await release(h, "work");
    const events = await collect(env.client, h.workflowId, {
      until: (e) => e.event.type === "turn_end" && e.turn_number === queued.turn_number,
    });
    const accepted = Object.fromEntries(
      events.flatMap((e) => (e.event.type === "message_accepted" ? [[e.message_id, e.event]] : [])),
    );
    assert.equal(accepted[work.message_id]!.disposition, "opened");
    assert.equal(accepted[queued.message_id]!.disposition, "queued");
    assertMatch(accepted[joined.message_id], {
      disposition: "joined",
      handler: "steer",
      payload: { text: "ride along" },
    });
    const order = events.map((e) => `${e.message_id}:${e.event.type}`);
    assert.ok(
      order.indexOf(`${work.message_id}:message_accepted`) < order.indexOf("null:turn_started"),
    );
    assert.ok(
      order.indexOf("null:turn_started") < order.indexOf(`${joined.message_id}:message_accepted`),
    );
  });

  it("message_handler_start brackets only the handler, not the wait in the queue", async () => {
    const { h } = await holdATurnOpen();
    const queued = await send(h, "work", { text: "next" });
    const waiting = await collect(env.client, h.workflowId, {
      until: (e) => e.message_id === queued.message_id,
    });
    assert.deepStrictEqual(
      waiting.filter((e) => e.message_id === queued.message_id).map((e) => e.event.type),
      ["message_accepted"],
    );
    assert.deepStrictEqual(
      (await status(h)).pending_turns.map((p) => p.message_id),
      [queued.message_id],
    );
    await release(h, "work");
    const started = await collect(env.client, h.workflowId, {
      until: (e) => e.event.type === "message_handler_start" && e.message_id === queued.message_id,
    });
    assert.deepStrictEqual(
      started.filter((e) => e.message_id === queued.message_id).map((e) => e.event.type),
      ["message_accepted", "message_handler_start"],
    );
  });

  it("the message context distinguishes joining from opening", async () => {
    const joinedFlags = (h: WorkflowHandle) =>
      eventually(async () => {
        const joined = await h.query<boolean[]>("joined");
        return joined.length ? joined : null;
      });
    const idle = await env.start("MidTurnProbeAgent");
    assert.equal((await send(idle, "steer", { text: "fresh" })).disposition, "opened");
    assert.deepStrictEqual(await joinedFlags(idle), [false]);
    const { h, work } = await holdATurnOpen();
    assert.equal((await send(h, "steer", { text: "ride" })).turn_id, work.turn_id);
    assert.deepStrictEqual(await joinedFlags(h), [true]);
    await release(h, "work");
  });

  it("sendMessage fast-fails on a join instead of hanging", async () => {
    const { h, work } = await holdATurnOpen();
    const err = await rejection(agentClient(env, h).sendMessage("steer", { text: "ride along" }));
    assert.ok(err instanceof JoinedTurnError);
    const reply = err.reply;
    assertMatch(reply, { disposition: "joined", turn_id: work.turn_id });
    await release(h, "work");
    const events = await collect(env.client, h.workflowId);
    assert.equal(
      events.filter(
        (e) => e.message_id === reply.message_id && e.event.type === "message_handler_end",
      ).length,
      1,
    );
  });

  it("a joined handler keeps its turn after a sibling finishes (refcounted participants)", async () => {
    const { h, work } = await holdATurnOpen();
    assert.equal((await send(h, "outlive", { text: "after you" })).turn_id, work.turn_id);
    assertMatch(await status(h), { turn_participants: 2, pending_turns: [] });
    await release(h, "work");
    await eventually(async () => (await status(h)).turn_participants === 1);
    assertMatch(await status(h), { turn_participants: 1, turn_active: true });
    await release(h, "outlive");
    const types = ours(await collect(env.client, h.workflowId), work.turn_id).map(
      (e) => e.event.type,
    );
    assert.equal(await h.query<boolean>("published_after_sibling"), true);
    assert.equal(count(types, "turn_end"), 1);
    assert.equal(types.at(-1), "turn_end");
  });

  it("close drains an in-flight joined handler", async () => {
    const { h } = await holdATurnOpen();
    await send(h, "outlive", { text: "drain me" });
    await release(h, "work");
    await h.signal("close");
    await eventually(() => h.query<boolean>("closed"));
    assertMatch(await status(h), { turn_active: true, turn_participants: 1 });
    assert.equal((await h.describe()).status.name, "RUNNING");
    await release(h, "outlive");
    await h.result();
    assert.equal(await h.query<boolean>("published_after_sibling"), true);
  });
});
