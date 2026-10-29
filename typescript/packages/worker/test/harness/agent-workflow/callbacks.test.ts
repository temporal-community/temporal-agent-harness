// Callback tools: a tool with no body, fulfilled by the client through provide_callback_result.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import {
  ToolApprovalPolicy,
  type AgentEvent,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { CallbackResultError } from "../../../src/harness/agent-client.ts";
import { assertMatch } from "../../match.ts";
import { collect, one, replyText, send, startEnv, typesFor, type TestEnv } from "../../support.ts";
import { agentClient, rejection, status } from "./helpers.ts";

let env: TestEnv;
before(async () => {
  env = await startEnv("callbacks");
});
after(async () => {
  await env.stop();
});

describe("callback tools", () => {
  const until = (toolId: string) => (e: AgentEvent) =>
    e.event.type === "callback_requested" && e.event.tool_id === toolId;

  it("returns the client's result, with output_schema on the request", async () => {
    const h = await env.start("CallbackProbeAgent");
    await send(h, "act", { text: "echo" });
    const events = await collect(env.client, h.workflowId, {
      onEvent: async (e) => {
        if (e.event.type === "callback_requested" && e.event.tool_id === "cb-echo") {
          assert.deepStrictEqual(e.event.tool_input, { text: "hi" });
          assert.ok((e.event.output_schema.properties as Record<string, unknown>).value);
          await agentClient(env, h).provideCallbackResult("cb-echo", {
            result: { value: "hi-back" },
          });
        }
      },
    });
    assert.deepStrictEqual(typesFor(events, "cb-echo"), [
      "tool_start",
      "callback_requested",
      "callback_resolved",
      "tool_end",
    ]);
    assert.equal(one(events, "callback_resolved").outcome, "ok");
    assert.equal(replyText(events), "ok:hi-back");
  });

  it("accepts a scalar output", async () => {
    const h = await env.start("CallbackProbeAgent");
    await send(h, "act", { text: "note" });
    await collect(env.client, h.workflowId, { until: until("cb-note") });
    await agentClient(env, h).provideCallbackResult("cb-note", { result: "noted: buy milk" });
    assert.equal(replyText(await collect(env.client, h.workflowId)), "ok:noted: buy milk");
  });

  it("is gated like any tool, and a denied callback never requests fulfillment", async () => {
    const strict = [{ approval_policy: ToolApprovalPolicy.alwaysRequireHumanApproval() }];
    const h = await env.start("CallbackProbeAgent", strict);
    await send(h, "act", { text: "echo" });
    const events = await collect(env.client, h.workflowId, {
      onEvent: async (e) => {
        if (e.event.type === "tool_approval_requested")
          await agentClient(env, h).approveTool("cb-echo", { approved: true });
        if (e.event.type === "callback_requested")
          await agentClient(env, h).provideCallbackResult("cb-echo", {
            result: { value: "hi-back" },
          });
      },
    });
    assert.deepStrictEqual(typesFor(events, "cb-echo"), [
      "tool_approval_requested",
      "tool_approval_resolved",
      "tool_start",
      "callback_requested",
      "callback_resolved",
      "tool_end",
    ]);
    assert.equal(replyText(events), "ok:hi-back");

    const d = await env.start("CallbackProbeAgent", strict);
    await send(d, "act", { text: "echo" });
    const denied = await collect(env.client, d.workflowId, {
      onEvent: (e) =>
        e.event.type === "tool_approval_requested"
          ? agentClient(env, d).approveTool("cb-echo", { approved: false, reason: "nope" })
          : undefined,
    });
    assert.deepStrictEqual(typesFor(denied, "cb-echo"), [
      "tool_approval_requested",
      "tool_approval_resolved",
    ]);
    assert.equal(replyText(denied), "denied:nope");
  });

  it("validates the result against the declared output type", async () => {
    const h = await env.start("CallbackProbeAgent");
    await send(h, "act", { text: "echo" });
    await collect(env.client, h.workflowId, { until: until("cb-echo") });
    const bad = await rejection(
      agentClient(env, h).provideCallbackResult("cb-echo", { result: { nope: 1 } }),
    );
    assert.ok(bad instanceof CallbackResultError);
    assert.equal(bad.errorType, "MalformedCallbackResult");
    await agentClient(env, h).provideCallbackResult("cb-echo", { result: { value: "fixed" } });
    assert.equal(replyText(await collect(env.client, h.workflowId)), "ok:fixed");
  });

  it("surfaces a client-reported error to the model", async () => {
    const h = await env.start("CallbackProbeAgent");
    await send(h, "act", { text: "echo" });
    await collect(env.client, h.workflowId, { until: until("cb-echo") });
    await agentClient(env, h).provideCallbackResult("cb-echo", {
      error: "permission denied on the user's machine",
    });
    const events = await collect(env.client, h.workflowId);
    assert.equal(typesFor(events, "cb-echo").at(-1), "tool_error");
    assert.equal(one(events, "callback_resolved").outcome, "error");
    assert.equal(replyText(events), "error:permission denied on the user's machine");
  });

  it("times out an unfulfilled callback", async () => {
    const h = await env.start("CallbackProbeAgent");
    await send(h, "act", { text: "timeout" });
    const events = await collect(env.client, h.workflowId);
    assert.deepStrictEqual(typesFor(events, "cb-timeout"), [
      "tool_start",
      "callback_requested",
      "callback_resolved",
      "tool_error",
    ]);
    assert.equal(one(events, "callback_resolved").outcome, "timeout");
    assert.match(replyText(events), /^error:callback timed out/);
  });

  it("pending callbacks are visible in status, and providing a result is idempotent", async () => {
    const h = await env.start("CallbackProbeAgent");
    await send(h, "act", { text: "echo" });
    await collect(env.client, h.workflowId, { until: until("cb-echo") });
    const [pending] = (await status(h)).pending_callbacks;
    assertMatch(pending, {
      tool_id: "cb-echo",
      tool_name: "echo_tool",
      tool_input: { text: "hi" },
    });
    assert.ok((pending!.output_schema.properties as Record<string, unknown>).value);
    const unknown = await rejection(
      agentClient(env, h).provideCallbackResult("does-not-exist", { result: { value: "x" } }),
    );
    assert.ok(unknown instanceof CallbackResultError);
    assert.equal(unknown.errorType, "UnknownCallback");
    await agentClient(env, h).provideCallbackResult("cb-echo", { result: { value: "x" } });
    const dup = await rejection(
      agentClient(env, h).provideCallbackResult("cb-echo", { result: { value: "y" } }),
    );
    assert.ok(dup instanceof CallbackResultError);
    assert.equal(dup.errorType, "CallbackAlreadyResolved");
    await collect(env.client, h.workflowId);
    assert.deepStrictEqual((await status(h)).pending_callbacks, []);
  });

  it("close while pending fails the callback", async () => {
    const h = await env.start("CallbackProbeAgent");
    await send(h, "act", { text: "echo" });
    const waiting = await collect(env.client, h.workflowId, { until: until("cb-echo") });
    await h.signal("close");
    assert.ok(!waiting.some((e) => e.event.type === "tool_end"));
    await h.result();
    assert.equal(
      await h.query<string>("last_reply"),
      "error:agent closed before the callback result arrived",
    );
  });
});
