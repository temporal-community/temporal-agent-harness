// Subagents: a parent agent starting, messaging and stopping child agents through the tools
// subagentToolset makes, and the child's turn merged into the parent's stream.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import type { AgentEvent } from "@temporalio/agent-harness/harness/agent-protocol";
import type { AgentStreamOutput } from "../../../src/harness/agent-client.ts";
import { assertMatch } from "../../match.ts";
import { collect, one, replyText, send, startEnv, type TestEnv } from "../../support.ts";
import { agentClient, status } from "./helpers.ts";

let env: TestEnv;
before(async () => {
  env = await startEnv("subagents");
});
after(async () => {
  await env.stop();
});

describe("subagents", () => {
  it("start, message and stop a child agent through its toolset", async () => {
    const h = await env.start("SubagentParentAgent");
    await send(h, "delegate", { text: "hello" });
    const events = await collect(env.client, h.workflowId);
    assert.equal(replyText(events), "HELLO");
    const startedEv = one(events, "subagent_started");
    assert.match(startedEv.subagent_id, /^[0-9a-f]{6}-[0-9a-f]{6}$/);
    assert.ok(startedEv.subagent_id.startsWith(events[0]!.agent_id));
    assertMatch(one(events, "subagent_message_sent"), {
      subagent_id: startedEv.subagent_id,
      handler: "echo",
      subagent_turn: 1,
      from_offset: 0,
    });
    assertMatch(one(events, "subagent_reply_received"), {
      workflow_id: startedEv.workflow_id,
      subagent_turn: 1,
      outcome: "ok",
    });
    assert.equal(one(events, "subagent_stopped").workflow_id, startedEv.workflow_id);
    assert.deepStrictEqual((await status(h)).subagents, []);
    await env.client.workflow.getHandle(startedEv.workflow_id).result();
    // A turn longer than five start-to-close timeouts must not fail the parent's turn.
    const scheduled = (await h.fetchHistory()).events!.find(
      (e) => e.activityTaskScheduledEventAttributes,
    )!.activityTaskScheduledEventAttributes!;
    assert.equal(scheduled.activityType!.name, "run_subagent_turn");
    assert.equal(scheduled.retryPolicy!.maximumAttempts ?? 0, 0);
  });

  it("a child's handler error fails the tool call and closes the bracket with outcome error", async () => {
    const h = await env.start("SubagentParentAgent");
    await send(h, "delegate", { text: "fail" });
    const events = await collect(env.client, h.workflowId);
    assert.equal(replyText(events), "error:child failed on fail");
    assertMatch(one(events, "subagent_reply_received"), {
      handler: "fail",
      subagent_turn: 1,
      outcome: "error",
    });
  });

  // A child the parent stops in the same turn may complete before the merge reaches it, and a
  // completed workflow's stream cannot be read; ToolChildParentAgent leaves its child running.
  it("sendMessage merges the child's turn inside its message_sent / reply_received bracket", async () => {
    const h = await env.start("ToolChildParentAgent");
    const items: AgentStreamOutput[] = [];
    for await (const [item] of await agentClient(env, h).sendMessage("delegate", {
      text: "",
    }))
      items.push(item);
    assert.ok(items.every((m) => "event" in m));
    const merged = items as AgentEvent[];
    const parent = merged[0]!.agent_id;
    const open = merged.findIndex((m) => m.event.type === "subagent_message_sent");
    const close = merged.findIndex((m) => m.event.type === "subagent_reply_received");
    const child = merged.filter((m) => m.agent_id !== parent);
    assert.deepStrictEqual(
      child.map((m) => m.event.type),
      [
        "message_accepted",
        "turn_started",
        "message_handler_start",
        "tool_start",
        "tool_end",
        "message_handler_end",
        "turn_end",
      ],
    );
    const childTurn = merged.flatMap((m, i) =>
      m.agent_id !== parent && m.turn_number === 1 ? [i] : [],
    );
    assert.ok(childTurn.every((i) => open < i && i < close));
    assertMatch(merged.at(-1), { agent_id: parent, event: { type: "turn_end" } });
    assertMatch(
      merged.filter((m) => m.agent_id === parent && m.event.type === "message_handler_end")[0]!
        .event,
      { output: { text: "act:S" } },
    );
    // Closing the parent closes the child, which ends the merge's abandoned poll on it.
    const childId = one(items as AgentEvent[], "subagent_started").workflow_id;
    await h.terminate("test finished");
    await assert.rejects(env.client.workflow.getHandle(childId).result());
  });

  it("a required initData callback that returns null fails the start tool before any child starts", async () => {
    const h = await env.start("NullBriefParentAgent");
    await send(h, "delegate", { text: "" });
    const events = await collect(env.client, h.workflowId);
    const message = "initData returned null, but the brief subagent requires data.";
    assert.equal(replyText(events), `TypeError:${message}`);
    assert.equal(one(events, "tool_error").message, message);
    assert.ok(!events.some((e) => e.event.type === "subagent_started"));
    assert.deepStrictEqual((await status(h)).subagents, []);
    const children = (await h.fetchHistory()).events!.filter(
      (e) => e.startChildWorkflowExecutionInitiatedEventAttributes,
    );
    assert.deepStrictEqual(children, []);
  });
});
