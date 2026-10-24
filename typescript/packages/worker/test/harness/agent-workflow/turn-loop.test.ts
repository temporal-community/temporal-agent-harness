// The agent workflow's turn loop: routing, validation, the published interface, handler errors,
// update idempotency, and the agent's lifecycle and init data.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import type { ApplicationFailure } from "@temporalio/common";
import { WorkflowFailedError, type WorkflowHandle } from "@temporalio/client";
import {
  AGENT_INTERFACE_QUERY,
  ToolApprovalPolicy,
  type AcceptedFunction,
} from "@temporalio/agent-harness/harness/agent-protocol";
import {
  AgentTurnError,
  AgentTurnTimeout,
  type AgentStreamOutput,
} from "../../../src/harness/agent-client.ts";
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
import { agentClient, rejection, status, updateFailure } from "./helpers.ts";

let env: TestEnv;
before(async () => {
  env = await startEnv("turn-loop");
});
after(async () => {
  await env.stop();
});

describe("the turn loop", () => {
  const seen = (h: WorkflowHandle, count: number) =>
    eventually(async () => {
      const s = await h.query<string[]>("seen");
      return s.length >= count ? s : null;
    });

  it("routes by type and reconstructs the input", async () => {
    const h = await env.start("TypedProbeAgent");
    await send(h, "greet", { name: "Ada" });
    await send(h, "pick", { model: "opus" });
    assert.deepStrictEqual(await seen(h, 2), ["greet:Ada", "pick:opus"]);
  });

  for (const type of ["does_not_exist", "__proto__", "constructor", "toString"])
    it(`rejects an unknown function (${type}) in the validator, admitting nothing`, async () => {
      const h = await env.start("TypedProbeAgent");
      const cause = await updateFailure(send(h, type, {}));
      assert.equal(cause.type, "UnknownFunction");
      assert.match(cause.message, /greet/);
      assert.match(cause.message, /pick/);
      const s = await status(h);
      assert.deepStrictEqual([s.current_turn, s.pending_turns], [0, []]);
    });

  it("rejects a malformed payload", async () => {
    const h = await env.start("TypedProbeAgent");
    const cause = await updateFailure(send(h, "greet", {}));
    assert.equal(cause.type, "MalformedMessage");
    assert.ok(cause.message.includes("'greet' does not match its input model"));
  });

  const deep = (n: number): unknown => (n === 0 ? "leaf" : { d: deep(n - 1) });
  for (const [name, payload, text] of [
    ["a lone surrogate", { name: "a\ud800b" }, "lone surrogate"],
    ["nesting past 200 levels", { name: "Ada", extra: deep(210) }, "deeper than 200"],
    ["an integer past 2^53", { name: "Ada", extra: 2 ** 60 }, "send it as a string"],
  ] as const)
    it(`rejects a payload a Python peer can't read back: ${name}`, async () => {
      const h = await env.start("TypedProbeAgent");
      const cause = await updateFailure(send(h, "greet", payload));
      assert.equal(cause.type, "NonPortableJson");
      assert.ok(cause.message.includes(text));
    });

  it("agent_interface announces every handler with its schemas, mid_turn and model_callable", async () => {
    const typed = await (
      await env.start("TypedProbeAgent")
    ).query<AcceptedFunction[]>(AGENT_INTERFACE_QUERY);
    const byName = Object.fromEntries(typed.map((f) => [f.name, f]));
    assert.deepStrictEqual(Object.keys(byName).sort(), ["boom", "greet", "pick"]);
    assert.equal(byName.greet!.description, "Greet a person by name.");
    assert.ok("name" in (byName.greet!.parameters.properties as object));
    assert.ok("message" in (byName.greet!.output.properties as object));

    const midTurn = Object.fromEntries(
      (
        await (
          await env.start("MidTurnProbeAgent")
        ).query<AcceptedFunction[]>(AGENT_INTERFACE_QUERY)
      ).map((f) => [f.name, f]),
    );
    assert.deepStrictEqual(
      [midTurn.work!.mid_turn, midTurn.steer!.mid_turn, midTurn.exclusive!.mid_turn],
      ["enqueue", "accept", "reject"],
    );
    assert.ok(Object.values(midTurn).every((f) => f.model_callable));
    assert.deepStrictEqual(Object.keys(midTurn.steer!.parameters.properties as object), ["text"]);
  });

  it("the reply is the handler's return value", async () => {
    const h = await env.start("TypedProbeAgent");
    await send(h, "greet", { name: "Ada" });
    assert.deepStrictEqual(
      one(await collect(env.client, h.workflowId), "message_handler_end").output,
      { message: "hi Ada" },
    );
  });

  it("an activity tool that keeps failing publishes tool_start and tool_error on each of its attempts", async () => {
    const h = await env.start("ToolRetryAgent");
    await send(h, "act", { text: "x" });
    const events = await collect(env.client, h.workflowId);
    assert.equal(replyText(events), "caught:ActivityFailure");
    assert.deepStrictEqual(typesFor(events, "t1"), [
      "tool_start",
      "tool_error",
      "tool_start",
      "tool_error",
      "tool_start",
      "tool_error",
    ]);
    for (const e of events.filter((e) => e.event.type === "tool_error"))
      assert.equal((e.event as { message: string }).message, "tool broke");
    const started = (await h.fetchHistory()).events!.flatMap((e) =>
      e.activityTaskStartedEventAttributes ? [e.activityTaskStartedEventAttributes.attempt] : [],
    );
    assert.deepStrictEqual(started, [3]);
  });

  it("an activity tool heartbeats, so it may run past its heartbeat timeout", async () => {
    const h = await env.start("ToolRetryAgent");
    await send(h, "act", { text: "slow" });
    assert.equal(replyText(await collect(env.client, h.workflowId)), "slow:slow");
  });

  it("an activity tool that times out publishes no tool_error and no tool_end", async () => {
    const h = await env.start("ToolRetryAgent");
    await send(h, "act", { text: "timeout" });
    const events = await collect(env.client, h.workflowId);
    assert.equal(replyText(events), "caught:ActivityFailure");
    assert.deepStrictEqual(typesFor(events, "t1"), ["tool_start"]);
  });

  for (const scenario of ["own-timeout", "activity"])
    it(`a cancellation the handler observes (${scenario}) publishes message_handler_error and turn_end`, async () => {
      const h = await env.start("CancellationProbeAgent");
      await send(h, "act", { text: scenario });
      const types = (await collect(env.client, h.workflowId)).map((e) => e.event.type);
      assert.deepStrictEqual(types.slice(-2), ["message_handler_error", "turn_end"]);
      assert.equal((await h.describe()).status.name, "RUNNING");
    });

  it("an inline tool its caller's scope cancels publishes no tool_error", async () => {
    const h = await env.start("CancellationProbeAgent");
    await send(h, "act", { text: "inline" });
    const events = await collect(env.client, h.workflowId);
    assert.equal(replyText(events), "caught:CancelledFailure");
    assert.deepStrictEqual(typesFor(events, "t1"), ["tool_start"]);
  });

  it("tool_error and message_handler_error carry the error's own message, not its causes", async () => {
    const h = await env.start("CancellationProbeAgent");
    await send(h, "act", { text: "chained" });
    const events = await collect(env.client, h.workflowId);
    assert.equal(one(events, "tool_error").message, "outer");
    assert.equal(one(events, "message_handler_error").message, "outer");
  });

  it("sendMessage yields AgentTurnError for a handler error", async () => {
    const h = await env.start("TypedProbeAgent");
    const items: AgentStreamOutput[] = [];
    for await (const [item] of await agentClient(env, h).sendMessage("boom", { text: "x" }))
      items.push(item);
    const errors = items.filter((i) => i instanceof AgentTurnError);
    assert.equal(errors.length, 1);
    assert.equal(errors[0]!.message, "boom: x");
  });

  it("sendMessage yields AgentTurnTimeout when the turn outlasts its timeout", async () => {
    const h = await env.start("MidTurnProbeAgent");
    const items: AgentStreamOutput[] = [];
    for await (const [item] of await agentClient(env, h).sendMessage(
      "work",
      { text: "long" },
      { timeoutSeconds: 1 },
    ))
      items.push(item);
    assert.ok(items.at(-1) instanceof AgentTurnTimeout);
    assert.equal((await status(h)).turn_active, true);
  });

  it("a handler error publishes message_handler_error and the loop survives", async () => {
    const h = await env.start("TypedProbeAgent");
    await send(h, "boom", { text: "x" });
    assert.ok(
      one(await collect(env.client, h.workflowId), "message_handler_error").message.includes(
        "boom: x",
      ),
    );
    await send(h, "greet", { name: "Bob" });
    assert.ok((await seen(h, 1))?.includes("greet:Bob"));
  });

  it("a rejected auto mode policy fails the message, not the workflow", async () => {
    const h = await env.start("NoEvaluatorProbeAgent");
    await send(h, "enable_auto_mode", { text: "please" });
    assert.ok(
      one(await collect(env.client, h.workflowId), "message_handler_error").message.includes(
        "auto_mode_enabled=True",
      ),
    );
    assert.deepStrictEqual(
      (await status(h)).approval_policy,
      ToolApprovalPolicy.allowTools(["get_order"]),
    );
    assert.equal((await h.describe()).status.name, "RUNNING");
    await send(h, "greet", { name: "Bob" });
    assert.ok((await seen(h, 1))?.includes("greet:Bob"));
  });

  it("a resend under the same update id is one message", async () => {
    const h = await env.start("TypedProbeAgent");
    const first = await send(h, "greet", { name: "Ada" }, "send-1");
    assert.deepStrictEqual(await send(h, "greet", { name: "Ada" }, "send-1"), first);
    assert.notEqual(
      (await send(h, "greet", { name: "Ada" }, "send-2")).message_id,
      first.message_id,
    );
    assert.deepStrictEqual(await seen(h, 2), ["greet:Ada", "greet:Ada"]);
    const s = await eventually(async () => {
      const st = await status(h);
      return st.current_turn === 2 && !st.turn_active ? st : null;
    });
    assert.deepStrictEqual(s?.pending_turns, []);
  });
});

describe("lifecycle and init data", () => {
  const events = (h: WorkflowHandle) => h.query<string[]>("events");
  const waitFor = (h: WorkflowHandle, ...expected: string[]) =>
    eventually(async () => {
      const e = await events(h);
      return JSON.stringify(e.slice(0, expected.length)) === JSON.stringify(expected);
    });

  it("setup runs before the first message and teardown after close", async () => {
    const h = await env.start("LifecycleProbeAgent", [{}, { traveler: "Ada" }]);
    await send(h, "whoami", { text: "" });
    assert.equal(await waitFor(h, "setup", "whoami:Ada"), true);
    await h.signal("close");
    await h.result();
    assert.deepStrictEqual(await events(h), ["setup", "whoami:Ada", "teardown"]);
  });

  it("a caller that knows only AgentConfig can start an agent whose data is optional", async () => {
    const h = await env.start("LifecycleProbeAgent", [{}]);
    await send(h, "whoami", { text: "" });
    assert.equal(await waitFor(h, "setup", "whoami:nobody"), true);
  });

  for (const [args, message] of [
    [[{}, { wrong: 1 }], "invalid init data"],
    [[{ agent_id: "not a valid id!" }], "invalid AgentConfig"],
  ] as const)
    it(`invalid input fails the workflow instead of retrying forever (${JSON.stringify(args)})`, async () => {
      const h = await env.start("LifecycleProbeAgent", [...args]);
      const err = await rejection(h.result());
      assert.ok(err instanceof WorkflowFailedError);
      const cause = err.cause as ApplicationFailure;
      assert.equal(cause.type, "InvalidAgentInput");
      assert.ok(cause.message.includes(message));
    });

  it("teardown runs when the workflow is cancelled", async () => {
    const h = await env.start("LifecycleProbeAgent", [{}, { traveler: "Ada" }]);
    await send(h, "whoami", { text: "" });
    await waitFor(h, "setup", "whoami:Ada");
    await h.cancel();
    await assert.rejects(h.result());
    assert.equal((await events(h)).at(-1), "teardown");
  });
});
