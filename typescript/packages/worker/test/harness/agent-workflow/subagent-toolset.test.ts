// subagentToolset: which tools it makes for a child agent, and the checks it runs on its options.

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { SubagentToolPolicy, subagentToolset } from "@temporalio/agent-harness";
import {
  BriefedAgent,
  EchoChildAgent,
  LifecycleProbeAgent,
  NoEvaluatorProbeAgent,
} from "../../fixtures/workflows.ts";
import { assertMatch } from "../../match.ts";

describe("subagentToolset", () => {
  const names = (tools: { name: string }[]) => tools.map((t) => t.name);
  const toolset = (tools?: SubagentToolPolicy) =>
    subagentToolset(NoEvaluatorProbeAgent, {
      key: "p",
      taskQueue: "q",
      ...(tools ? { tools } : {}),
    });

  for (const [name, policy, expected] of [
    ["model-callable handlers by default", undefined, ["start_p", "p_greet", "stop_p"]],
    [
      "only the named handlers, model-callable or not",
      SubagentToolPolicy.allowOnly("enable_auto_mode"),
      ["start_p", "p_enable_auto_mode", "stop_p"],
    ],
    [
      "every handler when dangerously allowed",
      SubagentToolPolicy.dangerouslyAllowAll(),
      ["start_p", "p_enable_auto_mode", "p_greet", "stop_p"],
    ],
  ] as const)
    it(`exposes ${name}`, () => {
      assert.deepStrictEqual(names(toolset(policy)), expected);
    });

  it("rejects a policy that selects nothing", () => {
    assert.throws(() => toolset(SubagentToolPolicy.allowOnly("missing")), /no callable surface/);
  });

  it("each send tool takes the child's own input model", () => {
    const greet = toolset().find((t) => t.name === "p_greet")!;
    assertMatch(greet.parameters, {
      properties: {
        subagent: { type: "string" },
        message: { properties: { name: { type: "string" } }, required: ["name"] },
      },
    });
  });

  it("a child with required init data needs a callback", () => {
    assert.throws(
      () => subagentToolset(BriefedAgent, { key: "b", taskQueue: "q" }),
      /requires init data/,
    );
  });

  it("a child with no init data rejects one", () => {
    assert.throws(
      () => subagentToolset(EchoChildAgent, { key: "e", taskQueue: "q", initData: () => ({}) }),
      /takes no init data/,
    );
  });

  it("optional init data may be left out or supplied", () => {
    assert.ok(
      names(subagentToolset(LifecycleProbeAgent, { key: "l", taskQueue: "q" })).includes("start_l"),
    );
    assert.ok(
      names(
        subagentToolset(BriefedAgent, {
          key: "b",
          taskQueue: "q",
          initData: () => ({ topic: "x" }),
        }),
      ).includes("b_ask"),
    );
  });
});
