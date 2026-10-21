// Raw model streams: a model activity publishes its SDK's raw events, and the workflow translates
// them into turn_events, bracketing each attempt.

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, describe, it } from "node:test";
import { defaultPayloadConverter } from "@temporalio/common";
import { WorkflowStreamClient } from "@temporalio/workflow-streams/client";
import {
  encodePayloadWire,
  workflowStreamPublishSignal,
} from "@temporalio/workflow-streams/workflow";
import type { AgentEvent } from "@temporalio/agent-harness/harness/agent-protocol";
import { rawEventsTopic } from "@temporalio/agent-harness/ai-sdks/integration-helpers/raw-events";
import { collect, startEnv, type TestEnv } from "../../support.ts";
import { agentClient } from "./helpers.ts";

let env: TestEnv;
before(async () => {
  env = await startEnv("raw-streams");
});
after(async () => {
  await env.stop();
});

describe("raw model streams", () => {
  async function modelEvents(scenario: string) {
    const h = await env.start("RawStreamProbeAgent");
    const out: { type: string; text?: string; usage?: unknown }[] = [];
    for await (const [item] of await agentClient(env, h).sendMessage("run", { text: scenario })) {
      const e = (item as AgentEvent).event;
      if (e.type === "model_interaction_started") out.push({ type: e.type });
      if (e.type === "model_interaction_ended") out.push({ type: e.type, usage: e.usage });
      if (e.type === "reply_delta") out.push({ type: e.type, text: e.text });
      if (e.type === "message_handler_end")
        out.push({ type: e.type, text: (e.output as { text: string }).text });
    }
    return out;
  }

  it("close a retried model call's failed attempt before the retry streams", async () => {
    const events = await modelEvents("retry");
    assert.notEqual(events[5]?.usage ?? null, null);
    assert.deepStrictEqual(events, [
      { type: "model_interaction_started" },
      { type: "reply_delta", text: "attempt 1" },
      { type: "model_interaction_ended", usage: null },
      { type: "model_interaction_started" },
      { type: "reply_delta", text: "attempt 2" },
      { type: "model_interaction_ended", usage: events[5]?.usage },
      { type: "message_handler_end", text: "attempt 2" },
    ]);
  });

  it("close the bracket of a model call that fails for good", async () => {
    assert.deepStrictEqual(await modelEvents("abandon"), [
      { type: "model_interaction_started" },
      { type: "reply_delta", text: "attempt 1" },
      { type: "model_interaction_ended", usage: null },
      { type: "message_handler_end", text: "model call failed" },
    ]);
  });

  it("are translated in the workflow, so the log holds only turn_events", async () => {
    const h = await env.start("RawStreamProbeAgent");
    for await (const _ of await agentClient(env, h).sendMessage("run", { text: "retry" }));
    const topics = new Set<string>();
    for await (const item of WorkflowStreamClient.create(env.client, h.workflowId).subscribe(
      undefined,
      0,
      { resultType: true },
    )) {
      topics.add(item.topic);
      if (item.data !== null && (item.data as AgentEvent).event?.type === "turn_end") break;
    }
    assert.deepStrictEqual([...topics], ["turn_events"]);
  });

  it("drop a publisher's redelivered batch, as WorkflowStream does", async () => {
    const ctx = { agent_id: "a", turn_id: "t", turn_number: 1, message_id: null };
    const topic = rawEventsTopic("openai-agents", ctx, null, 0);
    const data = encodePayloadWire(
      defaultPayloadConverter.toPayload({ type: "output_text_delta", delta: "once" }),
    );
    const batch = { items: [{ topic, data }], publisher_id: "p", sequence: 1 };
    // With start, so the first batch is buffered in the run's first workflow task.
    const workflowId = `RawStreamProbeAgent-${randomUUID()}`;
    env.started.push(workflowId);
    const h = await env.client.workflow.signalWithStart("RawStreamProbeAgent", {
      taskQueue: env.taskQueue,
      workflowId,
      args: [{}],
      signal: workflowStreamPublishSignal,
      signalArgs: [batch],
    });
    await h.signal(workflowStreamPublishSignal, batch);
    const twice = encodePayloadWire(
      defaultPayloadConverter.toPayload({ type: "output_text_delta", delta: "twice" }),
    );
    await h.signal(workflowStreamPublishSignal, {
      items: [{ topic, data: twice }],
      publisher_id: "p",
      sequence: 2,
    });
    const deltas = (
      await collect(env.client, h.workflowId, {
        until: (e) => e.event.type === "reply_delta" && e.event.text === "twice",
      })
    ).flatMap((e) => (e.event.type === "reply_delta" ? [e.event.text] : []));
    assert.deepStrictEqual(deltas, ["once", "twice"]);
  });
});
