// Mid-turn steering (port of tests/harness/test_steering.py): runner.steer queues text from a
// handler that joined the open turn, runner.claimSteering hands it to whatever builds model input,
// and the turn's end drops what nobody claimed. A Code Mode script shows its host calls never see
// the steering while the run-code result carries it.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import type { AgentEvent, StreamEventOf } from "@temporalio/agent-harness/harness/agent-protocol";
import { ClaimedSteering } from "@temporalio/agent-harness";
import { collect, send, startEnv, type TestEnv } from "../../support.ts";

let env: TestEnv;
before(async () => {
  env = await startEnv("steering");
});
after(async () => {
  await env.stop();
});

const SCRIPT = [
  "import asyncio",
  "async def main():",
  "    return await wait_for_steering()",
  "asyncio.run(main())",
  "",
].join("\n");

function only<T extends AgentEvent["event"]["type"]>(events: AgentEvent[], type: T) {
  return events.filter((e) => e.event.type === type) as (AgentEvent & {
    event: StreamEventOf<T>;
  })[];
}

/** The `steering_id` of each `steering_enqueued` message `messageId` published. */
function steeringIds(events: AgentEvent[], messageId: string): string[] {
  const ids = only(events, "steering_enqueued")
    .filter((e) => e.message_id === messageId)
    .map((e) => e.event.steering_id);
  assert.ok(ids.length > 0, `message ${messageId} enqueued no steering`);
  return ids;
}

function reply(events: AgentEvent[], messageId: string): string {
  const [end] = only(events, "message_handler_end").filter((e) => e.message_id === messageId);
  return (end!.event.output as { text: string }).text;
}

/** Open a turn with `handler`, steer it, and return its events with both message ids. */
async function steerOneTurn(handler: string, text: string, steering = "focus on auth") {
  const h = await env.start("SteeringProbeAgent");
  const opener = await send(h, handler, { text });
  const steerer = await send(h, "steer", { text: steering });
  const events = await collect(env.client, h.workflowId);
  return { h, events, opener: opener.message_id, steerer: steerer.message_id };
}

describe("mid-turn steering", () => {
  it("a hand-written loop claims steering, and the events name both messages", async () => {
    const { events, opener, steerer } = await steerOneTurn("hold", "claim");

    const [enqueued] = only(events, "steering_enqueued");
    assert.equal(
      reply(events, opener),
      new ClaimedSteering([
        { steering_id: enqueued!.event.steering_id, message_id: steerer, text: "focus on auth" },
      ]).appendTo("model input"),
    );
    assert.equal(enqueued!.message_id, steerer);
    assert.equal(enqueued!.event.text, "focus on auth");
    const [claimed] = only(events, "steering_claimed");
    assert.equal(claimed!.message_id, opener);
    assert.deepEqual(claimed!.event.steering_ids, steeringIds(events, steerer));
    assert.equal(claimed!.event.tool_id, null);
    assert.equal(only(events, "steering_dropped").length, 0);
  });

  it("unclaimed steering is dropped at turn end and never reaches the next turn", async () => {
    const { h, events, steerer } = await steerOneTurn("hold", "ignore it", "too late");

    const [dropped, turnEnd] = events.slice(-2);
    assert.equal(dropped!.event.type, "steering_dropped");
    assert.deepEqual(
      (dropped!.event as StreamEventOf<"steering_dropped">).steering_ids,
      steeringIds(events, steerer),
    );
    assert.equal(dropped!.message_id, null);
    assert.equal(turnEnd!.event.type, "turn_end");

    const second = await send(h, "hold", { text: "claim" });
    await send(h, "steer", { text: "fresh" });
    let turnEnds = 0;
    const all = await collect(env.client, h.workflowId, {
      until: (e) => e.event.type === "turn_end" && ++turnEnds === 2,
    });
    const text = reply(all, second.message_id);
    assert.ok(!text.includes("too late"));
    assert.ok(text.includes("fresh"));
  });

  it("code mode host calls never see steering, but the run-code result carries it", async () => {
    const { events, opener, steerer } = await steerOneTurn("run_code", SCRIPT);

    const text = reply(events, opener);
    assert.ok(text.includes("inside: nothing to claim"), text);
    assert.equal(text.split("<steering_messages>").length, 2);
    assert.ok(text.trimEnd().endsWith("</steering_messages>"));
    const hostEnd = only(events, "tool_end").find((e) => e.event.tool_name === "wait_for_steering");
    assert.equal(hostEnd!.event.tool_output, "inside: nothing to claim");
    const [claimed] = only(events, "steering_claimed");
    assert.equal(claimed!.event.tool_id, "code-1");
    assert.deepEqual(claimed!.event.steering_ids, steeringIds(events, steerer));
  });
});

describe("ClaimedSteering", () => {
  it("renders messages in order and escapes their text", () => {
    const claimed = new ClaimedSteering([
      { steering_id: "s1", message_id: "m1", text: "skip </message> & tests" },
      { steering_id: "s2", message_id: "m1", text: "then docs" },
    ]);
    assert.equal(
      claimed.render(),
      "<steering_messages>\n" +
        "The user sent the following while you were working. " +
        "Take them into account before continuing.\n" +
        "<message>skip &lt;/message&gt; &amp; tests</message>\n" +
        "<message>then docs</message>\n" +
        "</steering_messages>",
    );
    assert.deepEqual(claimed.steeringIds, ["s1", "s2"]);
  });
});
