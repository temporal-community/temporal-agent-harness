import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";
import { MockActivityEnvironment } from "@temporalio/testing";
import { ApplicationFailure, defaultPayloadConverter } from "@temporalio/common";
import { ZodError } from "zod";
import {
  JEV_TOOL_APPROVAL_ACTIVITY,
  DEFAULT_JEV_MODEL,
  buildRequest,
  decide,
  jevEvaluator,
  type JevApprovalAnswer,
} from "@temporalio/agent-harness/ai-sdks/jev/evaluator";
import {
  AgentConfigSchema,
  AgentMessageSchema,
  AutoApprovalCriteria,
  ToolApprovalPolicy,
  type AgentEvent,
  type AgentStreamItem,
  type AutoApprovalContext,
  type AutoApprovalCriteriaSet,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { harnessActivities, jevToolApproval } from "../../../src/index.ts";

describe("ToolApprovalPolicy", () => {
  it("is a validated value: tools are deduped and sorted", () => {
    assert.deepStrictEqual(ToolApprovalPolicy.allowTools(["b", "a", "b"]).auto_approve_tools, [
      "a",
      "b",
    ]);
    assert.deepStrictEqual(ToolApprovalPolicy.alwaysRequireHumanApproval(), {
      dangerously_skip_all_approvals: false,
      auto_approve_inherently_safe: false,
      auto_approve_tools: [],
      auto_mode_enabled: false,
    });
  });

  it("makes auto mode over dangerously_skip_all unrepresentable", () => {
    assert.throws(
      () =>
        ToolApprovalPolicy.parse({ dangerously_skip_all_approvals: true, auto_mode_enabled: true }),
      ZodError,
    );
    assert.throws(
      () => ToolApprovalPolicy.withAutoMode(ToolApprovalPolicy.dangerouslySkipAll(), true),
      ZodError,
    );
    assert.equal(ToolApprovalPolicy.dangerouslySkipAll().auto_mode_enabled, false);
    assert.equal(
      ToolApprovalPolicy.withToolAllowed(ToolApprovalPolicy.autoMode(), "x").auto_mode_enabled,
      true,
    );
    const toggled = ToolApprovalPolicy.withAutoMode(
      ToolApprovalPolicy.withAutoMode(ToolApprovalPolicy.autoMode(), false),
      true,
    );
    assert.equal(toggled.auto_mode_enabled, true);
  });

  it("withChanges reaches every field and re-validates", () => {
    const policy = ToolApprovalPolicy.autoMode({ preApprovedTools: ["a"] });
    assert.deepStrictEqual(
      ToolApprovalPolicy.withChanges(policy, { auto_approve_inherently_safe: true }),
      ToolApprovalPolicy.parse({
        auto_approve_tools: ["a"],
        auto_approve_inherently_safe: true,
        auto_mode_enabled: true,
      }),
    );
    assert.deepStrictEqual(
      ToolApprovalPolicy.withChanges(policy, { auto_approve_tools: ["c", "b"] }).auto_approve_tools,
      ["b", "c"],
    );
    assert.deepStrictEqual(
      ToolApprovalPolicy.withChanges(policy, { auto_approve_tools: [] }).auto_approve_tools,
      [],
    );
    assert.deepStrictEqual(
      ToolApprovalPolicy.withChanges(policy, { auto_mode_enabled: false }).auto_approve_tools,
      ["a"],
    );
    assert.deepStrictEqual(
      ToolApprovalPolicy.withChanges(ToolApprovalPolicy.withChanges(policy, {}), {}),
      policy,
    );
    assert.throws(
      () => ToolApprovalPolicy.withChanges(policy, { dangerously_skip_all_approvals: true }),
      ZodError,
    );
    assert.equal(
      ToolApprovalPolicy.withChanges(ToolApprovalPolicy.dangerouslySkipAll(), {
        dangerously_skip_all_approvals: false,
        auto_mode_enabled: true,
      }).auto_mode_enabled,
      true,
    );
  });

  it("layers: skip-all, inherently safe, allow-list, then auto mode", () => {
    const unsafe = { inherentlySafe: false };
    assert.equal(
      ToolApprovalPolicy.autoApproves(ToolApprovalPolicy.dangerouslySkipAll(), "run_sql", unsafe),
      true,
    );
    assert.equal(
      ToolApprovalPolicy.autoApproves(ToolApprovalPolicy.allowInherentlySafe(), "t", {
        inherentlySafe: true,
      }),
      true,
    );
    assert.equal(
      ToolApprovalPolicy.autoApproves(ToolApprovalPolicy.alwaysRequireHumanApproval(), "t", {
        inherentlySafe: true,
      }),
      false,
    );
    assert.equal(
      ToolApprovalPolicy.autoApproves(
        ToolApprovalPolicy.allowTools(["get_order"]),
        "get_order",
        unsafe,
      ),
      true,
    );

    assert.equal(
      ToolApprovalPolicy.consultsAutoMode(
        ToolApprovalPolicy.alwaysRequireHumanApproval(),
        "x",
        unsafe,
      ),
      false,
    );
    assert.equal(
      ToolApprovalPolicy.consultsAutoMode(ToolApprovalPolicy.autoMode(), "run_sql", unsafe),
      true,
    );
    const preApproved = ToolApprovalPolicy.autoMode({ preApprovedTools: ["get_order"] });
    assert.equal(ToolApprovalPolicy.consultsAutoMode(preApproved, "get_order", unsafe), false);
    assert.equal(ToolApprovalPolicy.consultsAutoMode(preApproved, "run_sql", unsafe), true);
    assert.equal(
      ToolApprovalPolicy.consultsAutoMode(
        ToolApprovalPolicy.withToolAllowed(ToolApprovalPolicy.autoMode(), "run_sql"),
        "run_sql",
        unsafe,
      ),
      false,
    );
    const off = ToolApprovalPolicy.withAutoMode(preApproved, false);
    assert.equal(ToolApprovalPolicy.consultsAutoMode(off, "run_sql", unsafe), false);
    assert.deepStrictEqual(off.auto_approve_tools, ["get_order"]);
  });

  it("AgentConfig validates the agent id", () => {
    assert.deepStrictEqual(AgentConfigSchema.parse({}), {
      approval_policy: null,
      auto_approval_criteria: null,
      agent_id: null,
    });
    assert.equal(AgentConfigSchema.parse({ agent_id: "a1b2c3-d4e5f6" }).agent_id, "a1b2c3-d4e5f6");
    assert.equal(AgentConfigSchema.safeParse({ agent_id: "not a valid id!" }).success, false);
  });
});

const criteriaSet = AutoApprovalCriteria.criteriaSet;
const destructive = (s: Parameters<typeof criteriaSet>[0]) =>
  AutoApprovalCriteria.parse({ sets: { destructive: s }, default: "destructive" });
const DEFAULT_CRITERIA = destructive({
  effect: "Permanently destroys the named resource.",
  deny_when: ["the call deletes anything"],
  escalate_when: ["the call touches production"],
});

function ctx({
  declared = null,
  ...overrides
}: Partial<AutoApprovalContext> & { declared?: string | null } = {}): AutoApprovalContext {
  const criteria = overrides.criteria ?? DEFAULT_CRITERIA;
  const toolName = overrides.tool_name ?? "delete_account";
  const set: AutoApprovalCriteriaSet | null = AutoApprovalCriteria.forTool(
    criteria,
    toolName,
    declared,
  );
  if (set === null) throw new Error("the gate builds no context when no criteria set resolves");
  return {
    tool_name: toolName,
    tool_input: { account_id: "acct_9" },
    inherently_safe: false,
    tool_description: "Permanently delete a customer account.",
    criteria,
    criteria_version: 0,
    criteria_set: set,
    criteria_set_name: AutoApprovalCriteria.setNameFor(criteria, toolName, declared) ?? "",
    thresholds: AutoApprovalCriteria.thresholdsForSet(criteria, set),
    ...overrides,
  };
}

describe("AutoApprovalCriteria", () => {
  it("a runtime assignment outranks the tool's declared set, which outranks the catch-all", () => {
    const criteria = AutoApprovalCriteria.parse({
      sets: {
        financial: { effect: "Moves money." },
        read_only: { effect: "Reads a record." },
        cautious: { effect: "Unknown." },
      },
      tools: { issue_refund: "read_only" },
      default: "cautious",
    });
    assert.equal(
      AutoApprovalCriteria.setNameFor(criteria, "issue_refund", "financial"),
      "read_only",
    );
    assert.equal(
      AutoApprovalCriteria.forTool(criteria, "issue_refund", "financial")?.effect,
      "Reads a record.",
    );
    assert.equal(
      AutoApprovalCriteria.forTool(criteria, "pay", "financial")?.effect,
      "Moves money.",
    );
    assert.equal(AutoApprovalCriteria.forTool(criteria, "brand_new")?.effect, "Unknown.");
  });

  for (const [name, criteria] of [
    ["nothing configured at all", AutoApprovalCriteria.empty()],
    [
      "assigned set is not registered",
      AutoApprovalCriteria.parse({ tools: { delete_account: "typo" }, default: "cautious" }),
    ],
    [
      "set holds only a threshold",
      AutoApprovalCriteria.parse({ sets: { empty: { min_confidence: 0.99 } }, default: "empty" }),
    ],
  ] as const)
    it(`every way of having no rules resolves to nothing: ${name}`, () => {
      assert.equal(AutoApprovalCriteria.forTool(criteria, "delete_account"), null);
    });

  it("a dangling set name is still reported by name", () => {
    const criteria = AutoApprovalCriteria.parse({ tools: { delete_account: "financail" } });
    assert.equal(AutoApprovalCriteria.forTool(criteria, "delete_account"), null);
    assert.equal(AutoApprovalCriteria.setNameFor(criteria, "delete_account"), "financail");
  });

  it("thresholds come from the governing set, then the agent-wide default", () => {
    const criteria = AutoApprovalCriteria.parse({
      sets: { financial: { effect: "e", min_confidence: 0.95 }, read_only: { effect: "e" } },
      tools: { issue_refund: "financial", get_order: "read_only" },
      min_confidence: 0.7,
      escalate_if_irreversible_above: 0.4,
    });
    assert.deepStrictEqual(ctx({ tool_name: "issue_refund", criteria }).thresholds, [0.95, 0.4]);
    assert.deepStrictEqual(ctx({ tool_name: "get_order", criteria }).thresholds, [0.7, 0.4]);
  });

  it("criteria default to empty; withSet and withToolAssigned retarget without touching other sets", () => {
    const base = AutoApprovalCriteria.parse({
      sets: { cautious: { effect: "Unknown." } },
      default: "cautious",
    });
    const next = AutoApprovalCriteria.withToolAssigned(
      AutoApprovalCriteria.withSet(base, "read_only", criteriaSet({ effect: "Reads." })),
      "some_mcp_tool",
      "read_only",
    );
    assert.equal(AutoApprovalCriteria.forTool(next, "some_mcp_tool")?.effect, "Reads.");
    assert.equal(AutoApprovalCriteria.forTool(next, "anything_else")?.effect, "Unknown.");
    assert.deepStrictEqual(Object.keys(next.sets).sort(), ["cautious", "read_only"]);
    assert.equal(AutoApprovalCriteria.forTool(AutoApprovalCriteria.empty(), "anything"), null);
  });

  for (const name of ["constructor", "toString", "valueOf", "hasOwnProperty", "__proto__"])
    it(`a tool or set named ${name} is just a name`, () => {
      const criteria = AutoApprovalCriteria.parse({
        sets: { cautious: { effect: "Unknown." } },
        default: "cautious",
      });
      assert.equal(AutoApprovalCriteria.forTool(criteria, name)?.effect, "Unknown.");
      assert.equal(
        AutoApprovalCriteria.forTool(AutoApprovalCriteria.parse({ default: name }), "t"),
        null,
      );
      const named = AutoApprovalCriteria.parse(
        JSON.parse(`{"sets":{"${name}":{"effect":"Named."}},"tools":{"${name}":"${name}"}}`),
      );
      assert.equal(AutoApprovalCriteria.forTool(named, name)?.effect, "Named.");
      const assigned = AutoApprovalCriteria.withToolAssigned(
        AutoApprovalCriteria.withSet(
          AutoApprovalCriteria.empty(),
          name,
          criteriaSet({ effect: "Added." }),
        ),
        "t",
        name,
      );
      assert.equal(AutoApprovalCriteria.forTool(assigned, "t")?.effect, "Added.");
      assert.ok(Object.hasOwn(JSON.parse(JSON.stringify(assigned)).sets, name));
    });
});

describe("AgentMessageSchema", () => {
  it("keeps a __proto__ key in the payload as data", () => {
    const message = AgentMessageSchema.parse(
      JSON.parse('{"type":"t","payload":{"__proto__":{"a":1},"b":2}}'),
    );
    assert.equal(Object.hasOwn(message.payload, "__proto__"), true);
    assert.equal(JSON.stringify(message.payload), '{"__proto__":{"a":1},"b":2}');
  });
});

describe("jev buildRequest", () => {
  it("shows Jev the call and the governing criteria", () => {
    const request = buildRequest(ctx());
    assert.equal(request.model, DEFAULT_JEV_MODEL);
    assert.deepStrictEqual(request.state.approval_criteria, {
      what_calls_to_this_tool_do: "Permanently destroys the named resource.",
      deny_when: ["the call deletes anything"],
      escalate_when: ["the call touches production"],
    });
    assert.deepStrictEqual(request.state.tool, {
      name: "delete_account",
      description: "Permanently delete a customer account.",
      declared_inherently_safe: false,
    });
    assert.deepStrictEqual(request.state.call_arguments, { account_id: "acct_9" });
    assert.ok(!("agent_state" in request.state));
  });

  it("a rule the operator did not write is absent, not empty", () => {
    assert.deepStrictEqual(
      buildRequest(ctx({ criteria: destructive({ effect: "Reads a record." }) })).state
        .approval_criteria,
      {
        what_calls_to_this_tool_do: "Reads a record.",
      },
    );
  });

  it("the criteria operating context reaches the model", () => {
    const criteria = { ...destructive({ effect: "e" }), context: { environment: "prod" } };
    assert.deepStrictEqual(
      (buildRequest(ctx({ criteria })).state.approval_criteria as Record<string, unknown>)
        .operating_context,
      { environment: "prod" },
    );
  });

  it("accepts extra state as a mapping; jevEvaluator also takes a callable", () => {
    assert.deepStrictEqual(
      buildRequest(ctx(), { extraState: { env: "staging" } }).state.agent_state,
      {
        env: "staging",
      },
    );
    assert.ok(!("agent_state" in buildRequest(ctx(), { extraState: undefined }).state));
    const evaluator = jevEvaluator({ extraState: (c) => ({ tool: c.tool_name }) });
    assert.equal(typeof evaluator, "function");
    assert.equal(evaluator.evaluatorLabel, "jev_evaluator");
  });

  it("offers exactly the three verdicts and asks irreversibility separately", () => {
    const { questions } = buildRequest(ctx());
    assert.deepStrictEqual(Object.keys(questions).sort(), ["irreversible", "verdict"]);
    assert.equal(questions.verdict!.type, "choice");
    const options = questions.verdict!.criteria as Record<string, Record<string, string>>;
    assert.deepStrictEqual(Object.keys(options).sort(), ["approve", "deny", "escalate"]);
    for (const option of Object.values(options))
      assert.deepStrictEqual(Object.keys(option).sort(), ["not_for", "what", "when"]);
    assert.equal(questions.irreversible!.type, "noul");
  });
});

const answer = (overrides: Partial<JevApprovalAnswer> = {}): JevApprovalAnswer => ({
  model: "jev-1.0",
  request_id: "req_1",
  verdict: "approve",
  verdict_confidence: 0.95,
  verdict_probabilities: {},
  irreversible: 0.02,
  input_tokens: null,
  output_tokens: null,
  ...overrides,
});
const run = (a: JevApprovalAnswer, opts: Partial<Parameters<typeof decide>[1]> = {}) =>
  decide(a, { minConfidence: 0.8, escalateIfIrreversibleAbove: 0.5, ...opts });

describe("jev decide", () => {
  it("a confident approve of a reversible call is approved", () => {
    const d = run(answer());
    assert.equal(d.verdict, "approved");
    assert.ok(d.reason?.includes("jev/jev-1.0"));
    assert.ok(d.reason?.includes("95%"));
  });

  it("a confident deny is denied, an explicit escalate goes to a human", () => {
    const deny = run(answer({ verdict: "deny", verdict_confidence: 0.9 }));
    assert.equal(deny.verdict, "denied");
    assert.ok(deny.reason?.includes("the approval criteria forbid this call"));
    assert.equal(
      run(answer({ verdict: "escalate", verdict_confidence: 0.9 })).verdict,
      "user-approval",
    );
  });

  for (const verdict of ["approve", "deny", "escalate"])
    it(`low confidence escalates whatever the verdict: ${verdict}`, () => {
      const d = run(answer({ verdict, verdict_confidence: 0.6 }));
      assert.equal(d.verdict, "user-approval");
      assert.ok(d.reason?.includes("below the 80% confidence"));
    });

  it("an irreversible call is escalated even when the criteria allow it, unless the ceiling is off", () => {
    const d = run(answer({ verdict_confidence: 0.99, irreversible: 0.8 }));
    assert.equal(d.verdict, "user-approval");
    assert.ok(d.reason?.includes("irreversible"));
    assert.equal(
      run(answer({ verdict_confidence: 0.99, irreversible: 0.99 }), {
        escalateIfIrreversibleAbove: null,
      }).verdict,
      "approved",
    );
  });

  it("irreversibility never rescues a deny or creates an approve; unknown verdicts escalate", () => {
    assert.equal(run(answer({ verdict: "deny", irreversible: 0 })).verdict, "denied");
    assert.equal(run(answer({ verdict: "escalate", irreversible: 0 })).verdict, "user-approval");
    assert.equal(
      run(answer({ verdict: "maybe", verdict_confidence: 0.99 })).verdict,
      "user-approval",
    );
  });
});

describe("jev_tool_approval activity", () => {
  const env = { ...process.env };
  const realFetch = globalThis.fetch;
  const stubEnv = (name: string, value: string) => void (process.env[name] = value);
  afterEach(() => {
    globalThis.fetch = realFetch;
    for (const name of ["TYPESAFE_API_KEY", "TYPESAFE_AI_API_KEY"]) {
      if (env[name] === undefined) delete process.env[name];
      else process.env[name] = env[name];
    }
  });

  it("is registered unconditionally with the harness activities", () => {
    assert.ok(Object.keys(harnessActivities()).includes(JEV_TOOL_APPROVAL_ACTIVITY));
  });

  it("fails non-retryably on a worker with no TypeSafe key", async () => {
    stubEnv("TYPESAFE_API_KEY", "");
    stubEnv("TYPESAFE_AI_API_KEY", "");
    const err = await new MockActivityEnvironment()
      .run(jevToolApproval, buildRequest(ctx()))
      .catch((e: unknown) => e);
    assert.ok(err instanceof ApplicationFailure);
    assert.equal((err as ApplicationFailure).type, "JevNotConfigured");
    assert.equal((err as ApplicationFailure).nonRetryable, true);
  });

  it("projects TypeSafe's answer into the typed result", async () => {
    stubEnv("TYPESAFE_API_KEY", "k");
    const seen: Record<string, unknown>[] = [];
    globalThis.fetch = async (_url: string | URL | Request, init?: RequestInit) => {
      seen.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return Response.json({
        model: "jev-1.0",
        request_id: "req_42",
        usage: { input_tokens: 11, output_tokens: 2 },
        answers: {
          verdict: {
            type: "choice",
            choice: "deny",
            confidence: 0.91,
            probabilities: { approve: 0.04, deny: 0.91, escalate: 0.05 },
          },
          irreversible: { type: "noul", noul: 0.77 },
        },
      });
    };
    const request = buildRequest(ctx());
    const result: JevApprovalAnswer = await new MockActivityEnvironment().run(
      jevToolApproval,
      request,
    );
    assert.deepStrictEqual(seen[0], {
      state: request.state,
      questions: request.questions,
      model: DEFAULT_JEV_MODEL,
    });
    assert.deepStrictEqual(result, {
      model: "jev-1.0",
      request_id: "req_42",
      verdict: "deny",
      verdict_confidence: 0.91,
      verdict_probabilities: { approve: 0.04, deny: 0.91, escalate: 0.05 },
      irreversible: 0.77,
      input_tokens: 11,
      output_tokens: 2,
    });
    assert.equal(run(result).verdict, "denied");
  });
});

const EVENTS: AgentStreamItem[] = [
  {
    type: "message_accepted",
    handler: "ask",
    payload: { text: "what's the weather in boston?" },
    disposition: "opened",
  },
  { type: "message_handler_start" },
  { type: "message_handler_end", output: { text: "It's sunny" } },
  { type: "message_handler_error", message: "boom" },
  { type: "turn_started" },
  { type: "turn_end" },
  { type: "reply_delta", text: "It's sunny" },
  { type: "thought_summary", delta: { summary: "thinking" } },
  { type: "text_annotation", delta: { annotation: { url: "https://example.com" } } },
  {
    type: "tool_requested",
    tool_id: "call_1",
    tool_name: "get_weather",
    tool_input: { city: "boston" },
  },
  {
    type: "tool_start",
    tool_id: "call_1",
    tool_name: "get_weather",
    tool_input: { city: "boston" },
  },
  { type: "tool_end", tool_id: "call_1", tool_name: "get_weather", tool_output: "72F sunny" },
  { type: "model_interaction_started", model: "gpt-5.1" },
  { type: "model_interaction_ended", model: "gpt-5.1", usage: null },
  { type: "state_patch", state_id: "plan", version: 1, ops: [{ op: "remove", path: "/steps/0" }] },
];

describe("event serialization", () => {
  for (const event of EVENTS)
    it(`${event.type} round-trips through the default converter with its type`, () => {
      const envelope: AgentEvent = {
        agent_id: "a1b2c3",
        turn_id: "turn-1",
        turn_number: 1,
        message_id: "msg-1",
        timestamp: 1234.5,
        event,
      };
      const restored = defaultPayloadConverter.fromPayload<AgentEvent>(
        defaultPayloadConverter.toPayload(envelope),
      );
      assert.equal(restored.event.type, event.type);
      assert.deepStrictEqual(restored, envelope);
    });
});
