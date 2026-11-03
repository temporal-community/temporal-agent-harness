// close_after_idle: the runner closing an agent that has sat idle for its configured time, the
// caller's AgentConfig overriding the agent's default, the agent_closed event and
// AgentStatus.close_reason, the AgentClosed rejection of messages sent after close, subagents never
// closing on idle, and a parent dropping a subagent something else closed. The dev server runs in
// real time, so only one test waits for the idle clock to run out; the rest read the clock off the
// workflow's history, where it is a timer started when the agent goes idle.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import type { WorkflowHandle } from "@temporalio/client";
import {
  CloseReason,
  type StreamEventOf,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { collect, eventually, send, startEnv, type TestEnv } from "../../support.ts";
import { status, updateFailure } from "./helpers.ts";

let env: TestEnv;
before(async () => {
  env = await startEnv("close-after-idle");
});
after(async () => {
  await env.stop();
});

interface Timeline {
  started_at: number;
  closed_at: number | null;
}

const IDLE_DEFAULT_MS = 1000;
// Every wait below is on a workflow, so a regression that leaves one open fails here, not by hanging.
const BOUNDED = { timeout: 10_000 };
const timeline = (h: WorkflowHandle) => h.query<Timeline>("timeline");
const untilClosed = (h: WorkflowHandle) =>
  collect(env.client, h.workflowId, { until: (e) => e.event.type === "agent_closed" });
const untilTurnEnd = (h: WorkflowHandle, turnNumber: number) =>
  collect(env.client, h.workflowId, {
    until: (e) => e.event.type === "turn_end" && e.turn_number === turnNumber,
  });

/** Every timer the workflow has started, as milliseconds to fire, in history order. */
async function timers(h: WorkflowHandle): Promise<number[]> {
  const events = (await h.fetchHistory()).events ?? [];
  return events.flatMap((e) => {
    const timeout = e.timerStartedEventAttributes?.startToFireTimeout;
    return timeout ? [Number(timeout.seconds ?? 0) * 1000 + Number(timeout.nanos ?? 0) / 1e6] : [];
  });
}

/** Run one turn of the probe's `work` handler to its end. */
async function runOneTurn(h: WorkflowHandle): Promise<void> {
  const { turn_number } = await send(h, "work", {});
  await h.signal("release", "");
  await untilTurnEnd(h, turn_number);
}

describe("close after idle", () => {
  it("closes an agent with no messages once its idle time runs out", BOUNDED, async () => {
    const h = await env.start("IdleProbeAgent");
    const events = untilClosed(h);
    await h.result();

    const t = await timeline(h);
    const elapsed = t.closed_at! - t.started_at;
    assert.ok(elapsed >= IDLE_DEFAULT_MS && elapsed < IDLE_DEFAULT_MS + 1000, `${elapsed}ms`);
    assert.equal((await status(h)).close_reason, CloseReason.IDLE_TIMEOUT_EXPIRED);
    const last = (await events).at(-1)!;
    assert.deepStrictEqual(last.event, {
      type: "agent_closed",
      reason: CloseReason.IDLE_TIMEOUT_EXPIRED,
    });
    assert.deepStrictEqual([last.turn_id, last.turn_number, last.message_id], ["", 0, null]);
  });

  it("runs no idle clock while a turn is open, and starts it again when the turn ends", BOUNDED, async () => {
    const h = await env.start("IdleProbeAgent", [{ close_after_idle: 30 }]);
    const { turn_number } = await send(h, "work", {});
    await collect(env.client, h.workflowId, {
      until: (e) => e.event.type === "message_handler_start",
    });
    // Only the clock started while the agent sat idle before the message.
    assert.deepStrictEqual(await timers(h), [30_000]);

    await h.signal("release", "");
    await untilTurnEnd(h, turn_number);
    const restarted = await eventually(async () => {
      const started = await timers(h);
      return started.length === 2 ? started : null;
    });
    assert.deepStrictEqual(restarted, [30_000, 30_000]);
  });

  it("lets a caller's close_after_idle override the agent's default", BOUNDED, async () => {
    const h = await env.start("IdleProbeAgent", [{ close_after_idle: 30 }]);
    await runOneTurn(h);
    const started = await timers(h);
    assert.ok(started.length > 0 && started.every((ms) => ms === 30_000), String(started));
  });

  it("never closes on idle when a caller passes a close_after_idle of 0", BOUNDED, async () => {
    const h = await env.start("IdleProbeAgent", [{ close_after_idle: 0 }]);
    await runOneTurn(h);
    assert.deepStrictEqual(await timers(h), []);
    assert.equal((await status(h)).close_reason, null);
  });

  it("rejects messages sent after close with AgentClosed", BOUNDED, async () => {
    // Teardown holds the closed workflow open, so a send still reaches its validator.
    const h = await env.start("IdleProbeAgent", [{ close_after_idle: 0 }, { teardown_ms: 60_000 }]);
    const events = untilClosed(h);
    await h.signal("close");
    assert.deepStrictEqual((await events).at(-1)!.event, {
      type: "agent_closed",
      reason: CloseReason.REQUESTED,
    });

    const cause = await updateFailure(send(h, "work", {}));
    assert.equal(cause.type, "AgentClosed");
    const s = await status(h);
    assert.equal(s.close_reason, CloseReason.REQUESTED);
    assert.deepStrictEqual([s.current_turn, s.pending_turns], [0, []]);
  });
});

describe("close after idle for subagents", () => {
  async function startChild() {
    const parent = await env.start("IdleParentAgent", [{ close_after_idle: 0 }]);
    await send(parent, "start_child", {});
    const info = (await eventually(async () => (await status(parent)).subagents[0] ?? null))!;
    env.started.push(info.workflow_id);
    return { parent, subagent: info.subagent_id, child: env.client.workflow.getHandle(info.workflow_id) };
  }

  it("never closes a subagent on idle, whatever the child's own default", BOUNDED, async () => {
    const { child } = await startChild();
    // A query is answered only once the child has run its first workflow task, which is where
    // its 300 ms default would have started the idle clock.
    assert.equal((await status(child)).close_reason, null);
    assert.deepStrictEqual(await timers(child), []);
  });

  it("drops a subagent something else closed and tells the model to start a new one", BOUNDED, async () => {
    const { parent, subagent, child } = await startChild();
    await child.signal("close");
    await child.result();
    assert.equal((await status(child)).close_reason, CloseReason.REQUESTED);

    const { turn_number } = await send(parent, "ask_child", { subagent });
    const events = await untilTurnEnd(parent, turn_number);

    const reply = events.findLast((e) => e.event.type === "message_handler_end")!
      .event as StreamEventOf<"message_handler_end">;
    const text = (reply.output as { text: string }).text;
    assert.match(text, /has closed/);
    assert.match(text, /start_child/);
    assert.deepStrictEqual((await status(parent)).subagents, []);
    const stopped = events.filter((e) => e.event.type === "subagent_stopped");
    assert.deepStrictEqual(
      stopped.map((e) => (e.event as { subagent_id: string }).subagent_id),
      [subagent],
    );
  });
});
