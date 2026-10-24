// The builtin Jev evaluator as an agent's auto mode evaluator, against a fake jev_tool_approval
// activity (test/support.ts).

import assert from "node:assert/strict";
import { after, before, beforeEach, describe, it } from "node:test";
import type { WorkflowHandle } from "@temporalio/client";
import {
  ToolApprovalPolicy,
  type AgentEvent,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { INJECTED_SECRET } from "../../fixtures/workflows.ts";
import { assertMatch } from "../../match.ts";
import {
  collect,
  eventually,
  jev,
  one,
  replyText,
  send,
  startEnv,
  typesFor,
  type TestEnv,
} from "../../support.ts";
import { agentClient, status } from "./helpers.ts";

let env: TestEnv;
before(async () => {
  env = await startEnv("jev-evaluator");
});
after(async () => {
  await env.stop();
});

describe("the builtin Jev evaluator", () => {
  beforeEach(() => {
    jev.seen = [];
    jev.error = null;
  });
  const answer = (verdict: string, confidence: number, irreversible: number) => {
    jev.answer = {
      model: "jev-test",
      request_id: "req_e2e",
      verdict,
      verdict_confidence: confidence,
      verdict_probabilities: { [verdict]: confidence },
      irreversible,
      input_tokens: null,
      output_tokens: null,
    };
  };
  async function runOnce(
    text: string,
    { type = "act", config = {} }: { type?: string; config?: object } = {},
    decide?: (h: WorkflowHandle, e: AgentEvent) => unknown,
  ) {
    const h = await env.start("JevApprovalProbeAgent", [config]);
    await send(h, type, { text });
    return collect(env.client, h.workflowId, { onEvent: decide && ((e) => decide(h, e)) });
  }
  const humanDecides = (approved: boolean, reason: string) => (h: WorkflowHandle, e: AgentEvent) =>
    e.event.type === "tool_approval_requested"
      ? agentClient(env, h).approveTool("r1", { approved, reason })
      : undefined;

  it("an approve dispatches the tool with no human, and Jev sees the call and the criteria", async () => {
    answer("approve", 0.95, 0.1);
    const events = await runOnce("20");
    assert.equal(jev.seen.length, 1);
    const { state } = jev.seen[0]!;
    assert.deepStrictEqual(state.approval_criteria, {
      what_calls_to_this_tool_do: "Moves real money out to a customer. Cannot be taken back.",
      approve_when: ["the amount is under 50"],
      deny_when: ["the amount is 50 or more"],
      operating_context: { environment: "test" },
    });
    assert.equal(
      (state.tool as { description: string }).description,
      "Refund a customer. Moves real money.",
    );
    assert.deepStrictEqual(state.call_arguments, { amount: 20 });
    assert.deepStrictEqual(typesFor(events, "r1"), [
      "tool_approval_requested",
      "auto_approval_evaluation_started",
      "auto_approval_evaluation_ended",
      "tool_approval_resolved",
      "tool_start",
      "tool_end",
    ]);
    const started = one(events, "auto_approval_evaluation_started");
    const ended = one(events, "auto_approval_evaluation_ended");
    assert.deepStrictEqual(
      [started.evaluator, ended.evaluator],
      ["jev_evaluator", "jev_evaluator"],
    );
    assert.equal(started.evaluation_id, ended.evaluation_id);
    assert.equal(ended.verdict, "approve");
    assert.deepStrictEqual(ended.details, {
      model: "jev-test",
      request_id: "req_e2e",
      verdict: "approve",
      confidence: 0.95,
      probabilities: { approve: 0.95 },
      irreversible: 0.1,
      usage: { input_tokens: null, output_tokens: null },
      thresholds: { min_confidence: 0.8, escalate_if_irreversible_above: 0.5 },
      criteria_set: "financial",
      criteria_version: 0,
    });
    assert.equal(one(events, "tool_approval_resolved").approved, true);
    assert.ok(one(events, "tool_approval_resolved").reason?.includes("jev/jev-test"));
    assert.equal(replyText(events), "refunded:20");
  });

  it("a deny stops the call and tells the model why", async () => {
    answer("deny", 0.92, 0.9);
    const events = await runOnce("500");
    assert.deepStrictEqual(typesFor(events, "r1"), [
      "tool_approval_requested",
      "auto_approval_evaluation_started",
      "auto_approval_evaluation_ended",
      "tool_approval_resolved",
    ]);
    assertMatch(one(events, "auto_approval_evaluation_ended"), {
      verdict: "deny",
      details: { irreversible: 0.9 },
    });
    assert.equal(one(events, "tool_approval_resolved").approved, false);
    assert.match(replyText(events), /^denied:jev\/jev-test/);
  });

  it("an unsure verdict leaves the call to a human", async () => {
    answer("approve", 0.4, 0.1);
    const events = await runOnce("20", {}, humanDecides(false, "not today"));
    assertMatch(one(events, "tool_approval_resolved"), { approved: false, reason: "not today" });
    assert.equal(replyText(events), "denied:not today");
    const ended = one(events, "auto_approval_evaluation_ended");
    assertMatch(ended, {
      verdict: "escalate",
      details: { confidence: 0.4, thresholds: { min_confidence: 0.8 } },
    });
    assert.ok(ended.reason?.includes("below the 80% confidence"));
  });

  it("a failing Jev call escalates rather than approving", async () => {
    jev.error = "typesafe is down";
    const events = await runOnce("20", {}, humanDecides(true, "ok by me"));
    assert.deepStrictEqual(typesFor(events, "r1"), [
      "tool_approval_requested",
      "auto_approval_evaluation_started",
      "auto_approval_evaluation_error",
      "tool_approval_resolved",
      "tool_start",
      "tool_end",
    ]);
    const failed = one(events, "auto_approval_evaluation_error");
    assert.equal(
      failed.evaluation_id,
      one(events, "auto_approval_evaluation_started").evaluation_id,
    );
    assert.equal(failed.evaluator, "jev_evaluator");
    assert.ok(failed.message.includes("typesafe is down"));
    assertMatch(one(events, "tool_approval_resolved"), { approved: true, reason: "ok by me" });
    assert.equal(replyText(events), "refunded:20");
  });

  it("activityOptions merge over the defaults, so the retry cap and summary stay", async () => {
    answer("approve", 0.95, 0.1);
    const h = await env.start("JevTimeoutProbeAgent");
    await send(h, "act", { text: "20" });
    assert.equal(replyText(await collect(env.client, h.workflowId)), "refunded:20");
    const scheduled = (await h.fetchHistory()).events!.flatMap((e) => {
      const a = e.activityTaskScheduledEventAttributes;
      return a?.activityType?.name === "jev_tool_approval" ? [{ a, e }] : [];
    });
    assert.equal(scheduled.length, 1);
    const { a, e } = scheduled[0]!;
    assert.equal(a.retryPolicy?.maximumAttempts, 3);
    assert.equal(Number(a.startToCloseTimeout?.seconds), 10);
    assert.match(Buffer.from(e.userMetadata!.summary!.data!).toString(), /jev_approval/);
  });

  for (const [name, config] of [
    ["auto mode off", { approval_policy: ToolApprovalPolicy.alwaysRequireHumanApproval() }],
    ["no criteria", { auto_approval_criteria: {} }],
  ] as const)
    it(`${name} never reaches the evaluator`, async () => {
      answer("approve", 0.99, 0);
      const h = await env.start("JevApprovalProbeAgent", [config]);
      await send(h, "act", { text: "20" });
      await eventually(async () =>
        (await status(h)).pending_approvals.some((p) => p.tool_id === "r1"),
      );
      assert.deepStrictEqual(jev.seen, []);
      assert.equal((await status(h)).has_auto_approval_evaluator, true);
      const events = await collect(env.client, h.workflowId, {
        until: (e) => e.event.type === "tool_approval_requested",
      });
      assert.deepStrictEqual(typesFor(events, "r1"), ["tool_approval_requested"]);
    });

  for (const type of ["act_with_credential", "act_inline_with_credential"])
    it(`an injected parameter never reaches the evaluator (${type})`, async () => {
      answer("approve", 0.95, 0.1);
      const events = await runOnce("20", { type });
      assert.equal(jev.seen.length, 1);
      assert.deepStrictEqual(jev.seen[0]!.state.call_arguments, { amount: 20 });
      const serialized = JSON.stringify(jev.seen[0]);
      assert.ok(!serialized.includes(INJECTED_SECRET));
      assert.ok(!serialized.includes("api_token"));
      assert.equal(replyText(events), "refunded:20");
    });
});
