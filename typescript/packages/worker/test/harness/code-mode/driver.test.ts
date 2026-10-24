// Code Mode scripts on a running agent: host calls as durable tool calls, approvals, failures and
// timeouts as the model reads them.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import {
  TOOL_APPROVAL_UPDATE,
  type AgentEvent,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { TIMED_OUT_ERROR } from "../../../src/harness/code-mode/monty-stepper.ts";
import { collect, replyText, send, startEnv, typesFor, type TestEnv } from "../../support.ts";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const py = (body: string) =>
  `import asyncio\nasync def main():\n${body.replace(/^/gm, "    ")}\nasyncio.run(main())`;
const GATHER = py(`a, b = await asyncio.gather(search(origin="SFO", n=2), search(origin="BOS", n=1))
cheapest = min(a["items"] + b["items"], key=lambda i: i["price"])
print("cheapest:", cheapest["id"])
return cheapest`);

const searchOrigins = (events: AgentEvent[]) =>
  events.flatMap((e) =>
    e.event.type === "tool_start" && e.event.tool_name === "search"
      ? [(e.event.tool_input as { origin: string }).origin]
      : [],
  );

describe("Code Mode on a running agent", () => {
  let env: TestEnv;
  before(async () => {
    env = await startEnv("code-mode");
  });
  after(async () => {
    await env.stop();
  });

  const answer =
    (h: { workflowId: string }, approved: boolean, seen: string[] = []) =>
    async (e: AgentEvent) => {
      if (e.event.type === "tool_approval_requested") {
        seen.push(e.event.tool_name);
        await env.client.workflow.getHandle(h.workflowId).executeUpdate(TOOL_APPROVAL_UPDATE, {
          args: [
            { tool_id: e.event.tool_id, approved, ...(approved ? {} : { reason: "too pricey" }) },
          ],
        });
      }
    };

  it("runs concurrent host calls as durable tool calls, each with its own lifecycle and a uuid4 tool id", async () => {
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", { text: GATHER });
    const events = await collect(env.client, h.workflowId);
    assert.deepEqual(typesFor(events, "code-1"), ["tool_start", "tool_end"]);
    const searches = events.flatMap((e) =>
      e.event.type === "tool_start" && e.event.tool_name === "search" ? [e.event] : [],
    );
    assert.deepEqual(
      searches
        .map((e) => e.tool_input as { origin: string; n: number })
        .sort((a, b) => a.origin.localeCompare(b.origin)),
      [
        { origin: "BOS", n: 1 },
        { origin: "SFO", n: 2 },
      ],
    );
    for (const { tool_id } of searches) {
      assert.match(tool_id, UUID);
      assert.deepEqual(typesFor(events, tool_id), ["tool_start", "tool_end"]);
    }
    assert.equal(
      replyText(events),
      "output:\ncheapest: SFO-0\nresult: {'id': 'SFO-0', 'price': 100}",
    );
  });

  it("a host call goes through its own tool's approval gate", async () => {
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", { text: py('return await book(id="X1")') });
    const seen: string[] = [];
    const events = await collect(env.client, h.workflowId, { onEvent: answer(h, true, seen) });
    assert.deepEqual(seen, ["book"]);
    assert.equal(replyText(events), "result: 'BOOKED-X1'");
    assert.ok(events.some((e) => e.event.type === "tool_approval_resolved"));
  });

  it("a script that fails the type check comes back as a script error, running nothing", async () => {
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", { text: py('return await search(origin="SFO", n="three")') });
    const events = await collect(env.client, h.workflowId);
    assert.match(replyText(events), /^Script error \(MontyTypingError/);
    assert.deepEqual(searchOrigins(events), []);
  });

  it("a runaway script is stopped with a TimeoutError the model reads, once, and the session carries on", async () => {
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", { text: "while True:\n    pass" });
    const events = await collect(env.client, h.workflowId);
    assert.deepEqual(typesFor(events, "code-1"), ["tool_start", "tool_end"]);
    assert.equal(replyText(events), `Script error (${TIMED_OUT_ERROR})`);
    const attempts = (await h.fetchHistory())
      .events!.filter((e) => e.activityTaskStartedEventAttributes)
      .map((e) => e.activityTaskStartedEventAttributes!.attempt);
    assert.deepEqual(attempts, [1]);

    await send(h, "run", { text: GATHER });
    const next = await collect(env.client, h.workflowId, {
      until: (e) => e.event.type === "turn_end" && e.turn_number === 2,
    });
    assert.ok(replyText(next.filter((e) => e.turn_number === 2)).includes("cheapest: SFO-0"));
  });

  it("a runaway step after host calls ends the script with only the TimeoutError", async () => {
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", { text: py('await search(origin="SFO", n=1)\nwhile True:\n    pass') });
    assert.equal(
      replyText(await collect(env.client, h.workflowId)),
      `Script error (${TIMED_OUT_ERROR})`,
    );
  });

  it("a script catches a denied host call and carries on", async () => {
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", {
      text: py(
        'try:\n    await book(id="X1")\nexcept Exception as e:\n    print("fallback:", e)\nr = await search(origin="SFO", n=1)\nreturn r["items"][0]["id"]',
      ),
    });
    const events = await collect(env.client, h.workflowId, { onEvent: answer(h, false) });
    assert.equal(
      replyText(events),
      "output:\nfallback: ToolApprovalDenied: tool 'book' was not approved: too pricey\nresult: 'SFO-0'",
    );
  });

  it("an uncaught host failure ends the script with only the script error", async () => {
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", {
      text: py('print("before")\nawait search(origin="SFO", n=1)\nawait failing_tool(text="x")'),
    });
    assert.equal(
      replyText(await collect(env.client, h.workflowId)),
      "Script error (MontyRuntimeError: Exception: ApplicationError: tool broke)",
    );
  });

  it("a failed activity tool raises Exception('ApplicationError: <message>') in the script, as in Python", async () => {
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", {
      text: py(
        'try:\n    await failing_tool(text="x")\nexcept Exception as e:\n    return [type(e).__name__, str(e)]',
      ),
    });
    assert.equal(
      replyText(await collect(env.client, h.workflowId)),
      "result: ['Exception', 'ApplicationError: tool broke']",
    );
  });

  it("one failing call in a batch leaves the others to complete", async () => {
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", {
      text: py(`async def settle(call):
    try:
        return await call
    except Exception as e:
        return "failed: " + str(e)
return await asyncio.gather(settle(search(origin="SFO", n=1)), settle(failing_tool(text="x")), settle(search(origin="BOS", n=1)))`),
    });
    assert.match(
      replyText(await collect(env.client, h.workflowId)),
      /^result: \[\{'items': \[\{'id': 'SFO-0', 'price': 100\}\]\}, 'failed: .*tool broke.*', \{'items': \[\{'id': 'BOS-0', 'price': 100\}\]\}\]$/,
    );
  });

  it("a script that returns with host calls pending drops them, as in Python, and they never run", async () => {
    // A bare unawaited call is already a type error; calls kept in a value get past the checker.
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", {
      text: py(
        'await search(origin="SFO", n=1)\nlater = [book(id="X1"), search(origin="BOS", n=1)]\nreturn "done"',
      ),
    });
    const events = await collect(env.client, h.workflowId);
    assert.equal(replyText(events), "result: 'done'");
    assert.deepEqual(
      events
        .filter((e) => e.event.type === "tool_start" || e.event.type === "tool_approval_requested")
        .map((e) => e.event.type === "tool_start" && e.event.tool_name),
      ["run_code", "search"],
    );
  });

  it("a call awaited a step later is already running, and asyncio.sleep is a durable timer", async () => {
    const h = await env.start("CodeModeProbeAgent");
    await send(h, "run", {
      text: py(
        'later = search(origin="BOS", n=1)\nawait asyncio.sleep(0.2)\nfirst = await search(origin="SFO", n=1)\nr = await later\nreturn [first["items"][0]["id"], r["items"][0]["id"]]',
      ),
    });
    const events = await collect(env.client, h.workflowId);
    assert.equal(replyText(events), "result: ['SFO-0', 'BOS-0']");
    assert.deepEqual(searchOrigins(events), ["BOS", "SFO"]);
    const history = (await h.fetchHistory()).events ?? [];
    assert.ok(
      history
        .filter((e) => e.timerStartedEventAttributes?.startToFireTimeout)
        .map((e) => Number(e.timerStartedEventAttributes!.startToFireTimeout!.nanos) / 1e6)
        .includes(200),
    );
  });
});
