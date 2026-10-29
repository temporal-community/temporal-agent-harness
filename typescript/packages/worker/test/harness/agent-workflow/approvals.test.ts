// Tool approvals: the policy's static layers, the human gate, auto mode evaluators, remembered
// approvals, and changes to the policy and criteria at runtime.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import type { WorkflowHandle } from "@temporalio/client";
import {
  APPROVAL_SHORT_ID_LENGTH,
  ToolApprovalPolicy,
  type AgentEvent,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { ToolApprovalError } from "../../../src/harness/agent-client.ts";
import { EVALUATED_CRITERIA } from "../../fixtures/workflows.ts";
import { assertMatch } from "../../match.ts";
import {
  collect,
  eventually,
  one,
  replyText,
  send,
  startEnv,
  typesFor,
  type TestEnv,
} from "../../support.ts";
import { agentClient, rejection, status } from "./helpers.ts";

let env: TestEnv;
before(async () => {
  env = await startEnv("approvals");
});
after(async () => {
  await env.stop();
});

describe("tool approvals", () => {
  const approve = (
    h: WorkflowHandle,
    toolId: string,
    approved = true,
    reason?: string,
    remember?: boolean,
  ) =>
    agentClient(env, h).approveTool(toolId, {
      approved,
      ...(reason ? { reason } : {}),
      ...(remember ? { remember } : {}),
    });
  const onRequest =
    (h: WorkflowHandle, toolId: string, approved = true, reason?: string) =>
    (e: AgentEvent) =>
      e.event.type === "tool_approval_requested" && e.event.tool_id === toolId
        ? approve(h, toolId, approved, reason)
        : undefined;
  /** Collect a turn, deciding every gated call with `decide` the moment it is requested. */
  const collectDeciding = (h: WorkflowHandle, decide: (e: AgentEvent) => unknown) =>
    collect(env.client, h.workflowId, { onEvent: decide });
  const pendingIds = async (h: WorkflowHandle) =>
    (await status(h)).pending_approvals.map((p) => p.tool_id);
  const waitForRequests = (h: WorkflowHandle, ids: string[]) => {
    const requested = new Set<string>();
    return collect(env.client, h.workflowId, {
      until: (e) => {
        if (e.event.type === "tool_approval_requested") requested.add(e.event.tool_id);
        return ids.every((id) => requested.has(id));
      },
    });
  };
  const bracketPairs = (events: AgentEvent[], evaluator: string) => {
    const started = one(events, "auto_approval_evaluation_started");
    const terminals = events.flatMap((e) =>
      e.event.type === "auto_approval_evaluation_ended" ||
      e.event.type === "auto_approval_evaluation_superseded" ||
      e.event.type === "auto_approval_evaluation_error"
        ? [e.event]
        : [],
    );
    assert.equal(terminals.length, 1);
    assert.deepStrictEqual([started.evaluator, terminals[0]!.evaluator], [evaluator, evaluator]);
    assert.ok(started.evaluation_id);
    assert.equal(terminals[0]!.evaluation_id, started.evaluation_id);
  };
  const GATED = ["tool_approval_requested", "tool_approval_resolved", "tool_start", "tool_end"];
  const EVALUATED = [
    "tool_approval_requested",
    "auto_approval_evaluation_started",
    "auto_approval_evaluation_ended",
    "tool_approval_resolved",
  ];

  it("an approved tool executes after approval", async () => {
    const h = await env.start("ApprovalProbeAgent");
    await send(h, "act", { text: "single" });
    const events = await collectDeciding(h, onRequest(h, "g1"));
    assert.deepStrictEqual(typesFor(events, "g1"), GATED);
    assertMatch(one(events, "tool_approval_requested"), { tool_input: { text: "S" } });
    assert.equal(one(events, "tool_approval_resolved").approved, true);
    assert.equal(replyText(events), "act:S");
  });

  it("every event of a dispatch carries its message id; only the turn brackets do not", async () => {
    const h = await env.start("ApprovalProbeAgent");
    const reply = await send(h, "act", { text: "single" });
    const events = await collectDeciding(h, onRequest(h, "g1"));
    for (const e of events)
      assert.equal(
        e.message_id,
        ["turn_started", "turn_end"].includes(e.event.type) ? null : reply.message_id,
        e.event.type,
      );
    const seen = new Set(events.map((e) => e.event.type));
    for (const t of ["message_accepted", "message_handler_start", ...GATED, "message_handler_end"])
      assert.ok(seen.has(t as AgentEvent["event"]["type"]), t);
  });

  it("a denied tool does not execute", async () => {
    const h = await env.start("ApprovalProbeAgent");
    await send(h, "act", { text: "single" });
    const events = await collectDeciding(h, onRequest(h, "g1", false, "not allowed"));
    assert.deepStrictEqual(typesFor(events, "g1"), [
      "tool_approval_requested",
      "tool_approval_resolved",
    ]);
    assertMatch(one(events, "tool_approval_resolved"), {
      approved: false,
      reason: "not allowed",
    });
    assert.equal(replyText(events), "denied:not allowed");
  });

  for (const [name, policy, text, toolId, expected] of [
    [
      "an inherently safe tool under allow-safe",
      ToolApprovalPolicy.allowInherentlySafe(),
      "safe",
      "s1",
      "safe:S",
    ],
    [
      "an allow-listed tool",
      ToolApprovalPolicy.allowTools(["gated_activity_tool"]),
      "single",
      "g1",
      "act:S",
    ],
    [
      "anything under dangerously-skip-all",
      ToolApprovalPolicy.dangerouslySkipAll(),
      "single",
      "g1",
      "act:S",
    ],
  ] as const)
    it(`auto-approves ${name} with no approval events`, async () => {
      const h = await env.start("ApprovalProbeAgent", [{ approval_policy: policy }]);
      await send(h, "act", { text });
      const events = await collect(env.client, h.workflowId);
      assert.deepStrictEqual(typesFor(events, toolId), ["tool_start", "tool_end"]);
      assert.equal(replyText(events), expected);
    });

  it("always-require gates even an inherently safe tool", async () => {
    const h = await env.start("ApprovalProbeAgent");
    await send(h, "act", { text: "safe" });
    const events = await collectDeciding(h, onRequest(h, "s1"));
    assert.deepStrictEqual(typesFor(events, "s1"), GATED);
    assert.equal(replyText(events), "safe:S");
  });

  it("the caller's config policy overrides the agent default", async () => {
    const h = await env.start("ApprovalProbeAgent", [
      { approval_policy: ToolApprovalPolicy.dangerouslySkipAll() },
    ]);
    assertMatch(await status(h), {
      approval_policy: ToolApprovalPolicy.dangerouslySkipAll(),
      has_auto_approval_evaluator: false,
    });
  });

  it("a config policy Python's lax parsing accepts is read the same way", async () => {
    const h = await env.start("ApprovalProbeAgent", [
      { approval_policy: { auto_approve_inherently_safe: "yes", auto_mode_enabled: 0 } },
    ]);
    assert.deepStrictEqual(
      (await status(h)).approval_policy,
      ToolApprovalPolicy.allowInherentlySafe(),
    );
  });

  it("an inline workflow tool is gated too", async () => {
    const h = await env.start("ApprovalProbeAgent");
    await send(h, "act", { text: "workflow-tool" });
    const events = await collectDeciding(h, onRequest(h, "wf-1"));
    assert.deepStrictEqual(typesFor(events, "wf-1"), GATED);
    assert.equal(replyText(events), "wf:Z");
  });

  it("a custom evaluator approves what the policy did not", async () => {
    const h = await env.start("EvaluatorProbeAgent");
    assert.equal((await status(h)).has_auto_approval_evaluator, true);
    await send(h, "act", { text: "single" });
    const events = await collect(env.client, h.workflowId);
    assert.deepStrictEqual(typesFor(events, "g1"), [...EVALUATED, "tool_start", "tool_end"]);
    bracketPairs(events, "approveGatedActivityTool");
    assertMatch(one(events, "auto_approval_evaluation_ended"), {
      verdict: "approve",
      details: {},
    });
    assertMatch(one(events, "tool_approval_resolved"), {
      approved: true,
      reason: "auto-approved by the evaluator",
    });
    assert.equal(replyText(events), "act:S");
  });

  it("an async evaluator approves with its reason", async () => {
    const h = await env.start("VerdictEvaluatorProbeAgent");
    await send(h, "act", { text: "arg:ok" });
    const events = await collect(env.client, h.workflowId);
    assert.deepStrictEqual(typesFor(events, "g1"), [...EVALUATED, "tool_start", "tool_end"]);
    bracketPairs(events, "verdictEvaluator");
    assertMatch(one(events, "auto_approval_evaluation_ended"), {
      verdict: "approve",
      reason: "looks fine",
    });
    assertMatch(one(events, "tool_approval_resolved"), { approved: true, reason: "looks fine" });
    const ts = (t: string) => events.find((e) => e.event.type === t)!.timestamp;
    assert.ok(ts("auto_approval_evaluation_ended") >= ts("auto_approval_evaluation_started"));
    assert.equal(replyText(events), "act:ok");
  });

  it("an evaluator can deny outright", async () => {
    const h = await env.start("VerdictEvaluatorProbeAgent");
    await send(h, "act", { text: "arg:deny" });
    const events = await collect(env.client, h.workflowId);
    assert.deepStrictEqual(typesFor(events, "g1"), EVALUATED);
    assert.equal(one(events, "auto_approval_evaluation_ended").verdict, "deny");
    assertMatch(one(events, "tool_approval_resolved"), {
      approved: false,
      reason: "policy forbids 'deny'",
    });
    assert.equal(replyText(events), "denied:policy forbids 'deny'");
  });

  it("an evaluator that escalates leaves the call to the human gate", async () => {
    const h = await env.start("VerdictEvaluatorProbeAgent");
    await send(h, "act", { text: "arg:escalate" });
    const events = await collectDeciding(h, async (e) => {
      if (e.event.type === "tool_approval_requested") {
        await eventually(async () => (await pendingIds(h)).length === 1);
        await approve(h, "g1", true, "I'll allow it");
      }
    });
    assert.deepStrictEqual(typesFor(events, "g1"), [...EVALUATED, "tool_start", "tool_end"]);
    assertMatch(one(events, "tool_approval_resolved"), {
      approved: true,
      reason: "I'll allow it",
    });
    bracketPairs(events, "verdictEvaluator");
    assertMatch(one(events, "auto_approval_evaluation_ended"), {
      verdict: "escalate",
      reason: "not sure",
    });
    assert.equal(replyText(events), "act:escalate");
  });

  it("a human who decides first cancels the evaluator and supersedes its bracket", async () => {
    const h = await env.start("SlowEvaluatorProbeAgent");
    await send(h, "act", { text: "single" });
    const events = await collectDeciding(h, (e) =>
      e.event.type === "auto_approval_evaluation_started"
        ? approve(h, "g1", true, "human got there first")
        : undefined,
    );
    assert.deepStrictEqual(typesFor(events, "g1"), [
      "tool_approval_requested",
      "auto_approval_evaluation_started",
      "tool_approval_resolved",
      "auto_approval_evaluation_superseded",
      "tool_start",
      "tool_end",
    ]);
    bracketPairs(events, "denyOnceReleased");
    assert.equal(one(events, "auto_approval_evaluation_superseded").verdict, null);
    assert.equal(await h.query<boolean>("evaluator_cancelled"), true);
    assertMatch(one(events, "tool_approval_resolved"), {
      approved: true,
      reason: "human got there first",
    });
    assert.equal(replyText(events), "act:S");
  });

  for (const [name, workflowType, message] of [
    [
      "an evaluator returning the wrong type",
      "NonsenseEvaluatorProbeAgent",
      "must return an AutoApprovalDecision",
    ],
    ["a broken evaluator", "BrokenEvaluatorProbeAgent", "approver is broken"],
  ] as const)
    it(`${name} escalates instead of failing open`, async () => {
      const h = await env.start(workflowType);
      await send(h, "act", { text: "single" });
      const events = await collectDeciding(h, onRequest(h, "g1", false, "no"));
      assert.deepStrictEqual(typesFor(events, "g1"), [
        "tool_approval_requested",
        "auto_approval_evaluation_started",
        "auto_approval_evaluation_error",
        "tool_approval_resolved",
      ]);
      assert.ok(one(events, "auto_approval_evaluation_error").message.includes(message));
      assert.equal(replyText(events), "denied:no");
    });

  it("remember allow-lists the tool and cascades to the other pending call", async () => {
    const h = await env.start("ApprovalProbeAgent");
    await send(h, "act", { text: "concurrent" });
    await waitForRequests(h, ["act-A", "act-B"]);
    await approve(h, "act-A", true, undefined, true);
    const events = await collect(env.client, h.workflowId);
    assert.deepStrictEqual(
      new Set(events.flatMap((e) => (e.event.type === "tool_end" ? [e.event.tool_id] : []))),
      new Set(["act-A", "act-B"]),
    );
    const resolved = Object.fromEntries(
      events.flatMap((e) =>
        e.event.type === "tool_approval_resolved" ? [[e.event.tool_id, e.event]] : [],
      ),
    );
    assertMatch(resolved["act-A"], { approved: true, remember: true });
    assertMatch(resolved["act-B"], {
      approved: true,
      remember: false,
      reason: "auto-approved by updated policy",
    });
    assert.equal(replyText(events), "act:A|act:B");
    assert.ok((await status(h)).approval_policy.auto_approve_tools.includes("gated_activity_tool"));
  });

  it("the remembered resolution is causally ordered before the cascade", async () => {
    const h = await env.start("ApprovalProbeAgent");
    await send(h, "act", { text: "concurrent" });
    await waitForRequests(h, ["act-A", "act-B"]);
    await approve(h, "act-B", true, undefined, true);
    const events = await collect(env.client, h.workflowId);
    assert.deepStrictEqual(
      events.flatMap((e) => (e.event.type === "tool_approval_resolved" ? [e.event.tool_id] : [])),
      ["act-B", "act-A"],
    );
  });

  it("pending approvals are visible in status, each with a unique short id", async () => {
    const h = await env.start("ApprovalProbeAgent");
    await send(h, "act", { text: "concurrent" });
    await waitForRequests(h, ["act-A", "act-B"]);
    const pending = (await status(h)).pending_approvals;
    assert.deepStrictEqual(new Set(pending.map((p) => p.tool_id)), new Set(["act-A", "act-B"]));
    assertMatch(pending[0], { tool_name: "gated_activity_tool" });
    const shortIds = pending.map((p) => p.short_id);
    for (const id of shortIds)
      assert.match(id, new RegExp(`^[0-9a-f]{${APPROVAL_SHORT_ID_LENGTH}}$`));
    assert.equal(new Set(shortIds).size, shortIds.length);
    for (const p of pending) await approve(h, p.tool_id);
    await collect(env.client, h.workflowId);
    assert.deepStrictEqual(await pendingIds(h), []);
  });

  it("approval is idempotent: unknown and already-resolved ids are typed errors", async () => {
    const h = await env.start("ApprovalProbeAgent");
    await send(h, "act", { text: "single" });
    await waitForRequests(h, ["g1"]);
    const unknown = await rejection(approve(h, "does-not-exist"));
    assert.ok(unknown instanceof ToolApprovalError);
    assert.equal(unknown.errorType, "UnknownToolApproval");
    await approve(h, "g1");
    const dup = await rejection(approve(h, "g1", false));
    assert.ok(dup instanceof ToolApprovalError);
    assert.equal(dup.errorType, "ToolApprovalAlreadyResolved");
    await collect(env.client, h.workflowId);
  });

  it("the first-approved of concurrent calls executes first", async () => {
    const h = await env.start("ApprovalProbeAgent");
    await send(h, "act", { text: "concurrent" });
    await waitForRequests(h, ["act-A", "act-B"]);
    await approve(h, "act-B");
    let approvedA = false;
    const events = await collectDeciding(h, async (e) => {
      if (e.event.type === "tool_start" && e.event.tool_id === "act-B" && !approvedA) {
        approvedA = true;
        await approve(h, "act-A");
      }
    });
    assert.deepStrictEqual(
      events.flatMap((e) => (e.event.type === "tool_start" ? [e.event.tool_id] : [])),
      ["act-B", "act-A"],
    );
    assert.equal(replyText(events), "act:A|act:B");
  });

  it("close while pending auto-denies", async () => {
    const h = await env.start("ApprovalProbeAgent");
    await send(h, "act", { text: "single" });
    const waiting = await waitForRequests(h, ["g1"]);
    await h.signal("close");
    assert.ok(!waiting.some((e) => e.event.type === "tool_start"));
    await h.result();
    assert.equal(await h.query<string>("last_reply"), "denied:agent closed before approval");
  });

  const evaluated = (h: WorkflowHandle) => h.query<string[]>("seen");
  /** Send one message and return the events of the turn it lands in, deciding only that turn's requests. */
  const turn = async (
    h: WorkflowHandle,
    type: string,
    text: string,
    decide?: (e: AgentEvent) => unknown,
  ) => {
    const { turn_id } = await send(h, type, { text });
    const events = await collect(env.client, h.workflowId, {
      until: (e) => e.event.type === "turn_end" && e.turn_id === turn_id,
      onEvent: (e) => (e.turn_id === turn_id ? decide?.(e) : undefined),
    });
    return events.filter((e) => e.turn_id === turn_id);
  };

  it("a policy change mid-turn approves the pending calls it now allows, without the evaluator, and leaves the rest", async () => {
    const h = await env.start("PolicyProbeAgent");
    assert.equal((await status(h)).has_auto_approval_evaluator, true);
    await send(h, "act", { text: "pair" });
    await waitForRequests(h, ["g1", "wf-1"]);
    await send(h, "policy", { text: "allow:gated_activity_tool" });
    await eventually(async () => (await pendingIds(h)).join() === "wf-1");
    await approve(h, "wf-1");
    const events = await collect(env.client, h.workflowId);
    const resolved = events.flatMap((e) =>
      e.event.type === "tool_approval_resolved" ? [e.event] : [],
    );
    assertMatch(resolved, [
      { tool_id: "g1", approved: true, reason: "auto-approved by updated policy" },
      { tool_id: "wf-1", approved: true },
    ]);
    assert.equal(await h.query<string>("last_reply"), "act:S|wf:Z");
    assert.deepStrictEqual(await evaluated(h), []);
  });

  it("auto mode toggles at runtime without losing the allow list", async () => {
    const h = await env.start("PolicyProbeAgent", [
      {
        approval_policy: ToolApprovalPolicy.allowTools(["gated_workflow_tool"]),
        auto_approval_criteria: EVALUATED_CRITERIA,
      },
    ]);
    await turn(h, "policy", "auto:on");
    assert.deepStrictEqual(
      (await status(h)).approval_policy,
      ToolApprovalPolicy.autoMode({ preApprovedTools: ["gated_workflow_tool"] }),
    );
    assert.equal(replyText(await turn(h, "act", "pair")), "act:S|wf:Z");
    assert.deepStrictEqual(await evaluated(h), ["gated_activity_tool@0"]);

    await turn(h, "policy", "auto:off");
    const events = await turn(h, "act", "pair", onRequest(h, "g1"));
    assert.deepStrictEqual(typesFor(events, "g1"), GATED);
    assert.deepStrictEqual(typesFor(events, "wf-1"), ["tool_start", "tool_end"]);
    assert.deepStrictEqual(await evaluated(h), ["gated_activity_tool@0"]);
  });

  it("a criteria change at runtime brings a tool under auto mode and bumps the version the evaluator sees", async () => {
    const ungoverned = { ...EVALUATED_CRITERIA, default: null };
    const h = await env.start("PolicyProbeAgent", [
      { approval_policy: ToolApprovalPolicy.autoMode(), auto_approval_criteria: ungoverned },
    ]);
    assert.deepStrictEqual(
      typesFor(await turn(h, "act", "single", onRequest(h, "g1")), "g1"),
      GATED,
    );
    assert.deepStrictEqual(await evaluated(h), []);

    await turn(h, "policy", "assign:evaluated");
    assert.equal(replyText(await turn(h, "act", "single")), "act:S");
    await turn(h, "policy", "criteria");
    assert.equal(replyText(await turn(h, "act", "single")), "act:S");
    assert.deepStrictEqual(await evaluated(h), ["gated_activity_tool@1", "gated_activity_tool@2"]);
  });
});
