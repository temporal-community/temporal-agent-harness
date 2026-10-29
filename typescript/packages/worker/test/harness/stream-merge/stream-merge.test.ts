import assert from "node:assert/strict";
import { afterEach, describe, it, mock } from "node:test";
import { setTimeout as sleep } from "node:timers/promises";
import { ServiceError, type Client } from "@temporalio/client";
import { WorkflowStreamClient } from "@temporalio/workflow-streams/client";
import {
  Gates,
  mergeStream,
  selectLive,
  selectReplay,
  subscribe,
  type Cursor,
  type SelectPolicy,
  type ShouldStop,
} from "../../../src/harness/stream-merge/index.ts";
import type { AgentEvent, AgentStreamItem } from "@temporalio/agent-harness/harness/agent-protocol";
import { assertMatch } from "../../match.ts";

const ev = (
  agentId: string,
  turn: number,
  event: AgentStreamItem,
  turnId?: string,
): AgentEvent => ({
  agent_id: agentId,
  turn_id: turnId ?? `${agentId}-t${turn}`,
  turn_number: turn,
  message_id: null,
  timestamp: 0,
  event,
});
const ts = (a: string, t: number) => ev(a, t, { type: "turn_started" });
const te = (a: string, t: number) => ev(a, t, { type: "turn_end" });
const reply = (a: string, t: number) =>
  ev(a, t, { type: "message_handler_end", output: { ok: true } });
const sub = (child: string) => ({
  subagent_id: child.slice(0, 6),
  agent_key: "k",
  workflow_id: child,
});
const ms = (a: string, t: number, child: string, childTurn: number, fromOffset = 0) =>
  ev(a, t, {
    type: "subagent_message_sent",
    ...sub(child),
    handler: "f",
    subagent_turn: childTurn,
    from_offset: fromOffset,
  });
const rr = (a: string, t: number, child: string, childTurn: number) =>
  ev(a, t, {
    type: "subagent_reply_received",
    ...sub(child),
    handler: "f",
    subagent_turn: childTurn,
    outcome: "ok",
  });
const started = (a: string, t: number, child: string) =>
  ev(a, t, { type: "subagent_started", ...sub(child) });
const stopped = (a: string, t: number, child: string) =>
  ev(a, t, { type: "subagent_stopped", ...sub(child) });
const snapshot = (a: string) =>
  ev(a, 0, { type: "state_snapshot", state_id: "plan", version: 0, value: { steps: [] } }, "");
const tool = (a: string, t: number, id: string, start: boolean) =>
  ev(
    a,
    t,
    start
      ? { type: "tool_start", tool_id: id, tool_name: "x", tool_input: {} }
      : { type: "tool_end", tool_id: id, tool_name: "x", tool_output: "done" },
  );

interface FakeOptions {
  closes?: string[];
  failAfter?: Record<string, number>;
  liveTail?: Set<string>;
  drip?: Record<string, number>;
}

function fakeStreams(
  streams: Record<string, AgentEvent[]>,
  { closes, failAfter = {}, liveTail = new Set(), drip = {} }: FakeOptions,
) {
  mock.method(WorkflowStreamClient, "create", (_client: Client, workflowId: string) => {
    const events = streams[workflowId] ?? [];
    const subscribe = (_topic: unknown, fromOffset: number) => {
      const inner = (async function* () {
        for (let offset = fromOffset, n = 0; offset < events.length; offset++, n++) {
          if (failAfter[workflowId] !== undefined && n >= failAfter[workflowId])
            throw new Error("simulated stream read error");
          if (drip[workflowId] !== undefined) await sleep(drip[workflowId] * 1000);
          yield { topic: "turn_events", data: events[offset]!, offset };
        }
        if (liveTail.has(workflowId)) await new Promise(() => {});
      })();
      return {
        next: () => inner.next(),
        return: (v: undefined) => {
          closes?.push(workflowId);
          return inner.return(v);
        },
        [Symbol.asyncIterator]() {
          return this;
        },
      };
    };
    return { subscribe } as unknown as WorkflowStreamClient;
  });
}

/** A client whose calls cannot be aborted, recording each abort signal it is handed. */
const fakeClient = (signals: AbortSignal[] = []) =>
  ({
    withAbortSignal: (signal: AbortSignal, fn: () => unknown) => {
      signals.push(signal);
      return fn();
    },
  }) as unknown as Client;

async function runMerge(
  streams: Record<string, AgentEvent[]>,
  {
    select,
    skipUntilTurnId = null,
    rootFromOffset = 0,
    shouldStop = async () => false,
    stallGraceSeconds = 5,
    resumeOffsets,
    ...fake
  }: FakeOptions & {
    select: SelectPolicy;
    skipUntilTurnId?: string | null;
    rootFromOffset?: number;
    shouldStop?: ShouldStop;
    stallGraceSeconds?: number;
    resumeOffsets?: number[];
  },
): Promise<AgentEvent[]> {
  fakeStreams(streams, fake);
  const out: AgentEvent[] = [];
  for await (const [e, resume] of mergeStream({
    client: fakeClient(),
    rootWorkflowId: "P",
    rootFromOffset,
    skipUntilTurnId,
    select,
    shouldStop,
    stallGraceSeconds,
  })) {
    out.push(e);
    resumeOffsets?.push(resume);
  }
  return out;
}

/** Every stream intact and in order, and each subagent turn nested between its open and close. */
function assertValidMerge(merged: AgentEvent[], streams: Record<string, AgentEvent[]>) {
  for (const [wf, events] of Object.entries(streams))
    assert.deepStrictEqual(
      merged.filter((m) => m.agent_id === wf),
      events,
    );
  merged.forEach((m, i) => {
    if (m.event.type !== "subagent_reply_received") return;
    const { workflow_id: child, subagent_turn: t } = m.event;
    const j = merged.findIndex(
      (x) =>
        x.event.type === "subagent_message_sent" &&
        x.event.workflow_id === child &&
        x.event.subagent_turn === t,
    );
    assert.ok(j >= 0, `reply_received(${child},${t}) has no matching message_sent`);
    assert.ok(j < i);
    merged.forEach((x, k) => {
      if (x.agent_id === child && x.turn_number === t) {
        assert.ok(k > j);
        assert.ok(k < i);
      }
    });
  });
}

const types = (events: AgentEvent[]) => events.map((e) => e.event.type);
const parentOneChildTurn = () => ({
  P: [ts("P", 1), ms("P", 1, "C", 1), rr("P", 1, "C", 1), reply("P", 1), te("P", 1)],
  C: [ts("C", 1), tool("C", 1, "c1", true), tool("C", 1, "c1", false), reply("C", 1), te("C", 1)],
});
const bothPolicies = [
  ["replay", selectReplay],
  ["live", selectLive],
] as const;

afterEach(() => {
  mock.restoreAll();
});

describe("Gates", () => {
  it("the open gate holds a child until its message_sent is emitted", () => {
    const gates = new Gates();
    assert.equal(gates.ready(true, "C", ts("C", 1)), false);
    assert.deepStrictEqual(gates.onEmit(false, "P", ms("P", 1, "C", 1, 7)), {
      mount: { workflowId: "C", fromOffset: 7, subagentId: "C" },
    });
    assert.equal(gates.ready(true, "C", ts("C", 1)), true);
  });

  it("the close gate holds reply_received until the child's turn_end is emitted", () => {
    const gates = new Gates();
    gates.onEmit(false, "P", ms("P", 1, "C", 1));
    const r = rr("P", 1, "C", 1);
    assert.equal(gates.ready(false, "P", r), false);
    gates.onEmit(true, "C", te("C", 1));
    assert.equal(gates.ready(false, "P", r), true);
  });

  it("does not strand a child event that belongs to no turn, and never gates the root", () => {
    const gates = new Gates();
    assert.equal(gates.ready(true, "C", snapshot("C")), true);
    assert.equal(gates.ready(true, "C", ts("C", 1)), false);
    assert.equal(gates.ready(false, "P", ts("P", 1)), true);
  });

  it("onEmit mounts only for message_sent and unmounts for subagent_stopped", () => {
    const gates = new Gates();
    assert.equal(gates.onEmit(false, "P", ts("P", 1)), null);
    assert.equal(gates.onEmit(true, "C", te("C", 1)), null);
    assert.ok("mount" in (gates.onEmit(false, "P", ms("P", 1, "C", 1)) ?? {}));
    assert.deepStrictEqual(gates.onEmit(false, "P", stopped("P", 1, "C")), { unmount: "C" });
  });
});

describe("mergeStream", () => {
  for (const [policy, select] of bothPolicies)
    it(`nests a single subagent turn in its bracket (${policy})`, async () => {
      const streams = parentOneChildTurn();
      const merged = await runMerge(streams, { select });
      assertValidMerge(merged, streams);
      assert.equal(merged.length, 10);
    });

  for (const [policy, select] of bothPolicies)
    it(`a child state registered before any turn does not wedge its stream (${policy})`, async () => {
      const streams = parentOneChildTurn();
      streams.C = [snapshot("C"), ...streams.C];
      const merged = await runMerge(streams, { select });
      assertValidMerge(merged, streams);
      assert.equal(merged.length, 11);
      assert.ok(types(merged).includes("state_snapshot"));
    });

  it("replay is deterministic and reads parent-before-child", async () => {
    const a = await runMerge(parentOneChildTurn(), { select: selectReplay });
    const b = await runMerge(parentOneChildTurn(), { select: selectReplay });
    assert.deepStrictEqual(a, b);
    assert.deepStrictEqual(
      a.map((m) => [m.agent_id, m.event.type]),
      [
        ["P", "turn_started"],
        ["P", "subagent_message_sent"],
        ["C", "turn_started"],
        ["C", "tool_start"],
        ["C", "tool_end"],
        ["C", "message_handler_end"],
        ["C", "turn_end"],
        ["P", "subagent_reply_received"],
        ["P", "message_handler_end"],
        ["P", "turn_end"],
      ],
    );
  });

  for (const [policy, select] of bothPolicies)
    it(`a reused subagent runs two turns in one parent turn (${policy})`, async () => {
      const streams = {
        P: [
          ts("P", 1),
          ms("P", 1, "C", 1, 0),
          rr("P", 1, "C", 1),
          ms("P", 1, "C", 2, 3),
          rr("P", 1, "C", 2),
          te("P", 1),
        ],
        C: [ts("C", 1), reply("C", 1), te("C", 1), ts("C", 2), reply("C", 2), te("C", 2)],
      };
      assertValidMerge(await runMerge(streams, { select }), streams);
    });

  for (const [policy, select] of bothPolicies)
    it(`two concurrent subagents keep overlapping brackets (${policy})`, async () => {
      const streams = {
        P: [
          ts("P", 1),
          ms("P", 1, "C", 1),
          ms("P", 1, "D", 1),
          rr("P", 1, "C", 1),
          rr("P", 1, "D", 1),
          reply("P", 1),
          te("P", 1),
        ],
        C: [ts("C", 1), reply("C", 1), te("C", 1)],
        D: [ts("D", 1), reply("D", 1), te("D", 1)],
      };
      assertValidMerge(await runMerge(streams, { select }), streams);
    });

  for (const [policy, select] of bothPolicies)
    it(`recurses into a nested grandchild (${policy})`, async () => {
      const streams = {
        P: [ts("P", 1), ms("P", 1, "C", 1), rr("P", 1, "C", 1), te("P", 1)],
        C: [ts("C", 1), ms("C", 1, "G", 1), rr("C", 1, "G", 1), reply("C", 1), te("C", 1)],
        G: [ts("G", 1), reply("G", 1), te("G", 1)],
      };
      assertValidMerge(await runMerge(streams, { select }), streams);
    });

  const stopAtTarget: ShouldStop = async (cursor: Cursor, e: AgentEvent) =>
    !cursor.isChild && e.turn_id === "P-t2" && e.event.type === "turn_end";

  it("send-message skips the preamble up to the target turn_started", async () => {
    const t = "P-t2";
    const streams = {
      P: [
        ts("P", 1),
        reply("P", 1),
        te("P", 1),
        ev("P", 2, { type: "turn_started" }, t),
        ev("P", 2, { type: "message_handler_end", output: {} }, t),
        ev("P", 2, { type: "turn_end" }, t),
      ],
    };
    const merged = await runMerge(streams, {
      select: selectLive,
      skipUntilTurnId: t,
      rootFromOffset: 3,
      shouldStop: stopAtTarget,
    });
    assert.deepStrictEqual(types(merged), ["turn_started", "message_handler_end", "turn_end"]);
    assert.equal(
      merged.every((m) => m.turn_id === t),
      true,
    );
  });

  it("send-message resume mounts a reused child at its from_offset", async () => {
    const t = "P-t2";
    const streams = {
      P: [
        ts("P", 1),
        ms("P", 1, "C", 1, 0),
        rr("P", 1, "C", 1),
        te("P", 1),
        ev("P", 2, { type: "turn_started" }, t),
        ms("P", 2, "C", 2, 3),
        rr("P", 2, "C", 2),
        ev("P", 2, { type: "turn_end" }, t),
      ],
      C: [ts("C", 1), reply("C", 1), te("C", 1), ts("C", 2), reply("C", 2), te("C", 2)],
    };
    const merged = await runMerge(streams, {
      select: selectReplay,
      skipUntilTurnId: t,
      rootFromOffset: 4,
      shouldStop: stopAtTarget,
    });
    assert.equal(
      merged.every((m) => m.turn_number === 2),
      true,
    );
    const open = types(merged).indexOf("subagent_message_sent");
    const close = types(merged).indexOf("subagent_reply_received");
    const childAt = merged.flatMap((m, i) => (m.agent_id === "C" ? [i] : []));
    assert.ok(childAt.length > 0);
    assert.equal(
      childAt.every((i) => open < i && i < close),
      true,
    );
  });

  it("the stop predicate ends the merge at the root turn_end", async () => {
    const streams = parentOneChildTurn();
    const merged = await runMerge(streams, {
      select: selectReplay,
      shouldStop: async (c, e) => !c.isChild && e.event.type === "turn_end",
    });
    assertMatch(merged.at(-1), { agent_id: "P", event: { type: "turn_end" } });
    assertValidMerge(merged, streams);
  });

  it("an empty root stream yields nothing", async () => {
    assert.deepStrictEqual(await runMerge({ P: [] }, { select: selectReplay }), []);
  });

  for (const [policy, select] of bothPolicies)
    it(`unmounts a child cursor on subagent_stopped (${policy})`, async () => {
      const streams = {
        P: [
          ts("P", 1),
          started("P", 1, "C"),
          ms("P", 1, "C", 1),
          rr("P", 1, "C", 1),
          stopped("P", 1, "C"),
          te("P", 1),
        ],
        C: [ts("C", 1), reply("C", 1), te("C", 1)],
      };
      const closes: string[] = [];
      const merged = await runMerge(streams, { select, closes, liveTail: new Set(["C"]) });
      assertValidMerge(merged, streams);
      assert.ok(closes.includes("C"));
    });

  it("an unreadable child does not crash the merge and is marked unavailable once", async () => {
    const streams = parentOneChildTurn();
    const merged = await runMerge(streams, { select: selectReplay, failAfter: { C: 0 } });
    assert.deepStrictEqual(types(merged.filter((m) => m.agent_id === "P")), [
      "turn_started",
      "subagent_message_sent",
      "subagent_reply_received",
      "message_handler_end",
      "turn_end",
    ]);
    assert.deepStrictEqual(
      merged.filter((m) => m.agent_id === "C" && m.event.type !== "subagent_stream_unavailable"),
      [],
    );
    const markers = merged.filter((m) => m.event.type === "subagent_stream_unavailable");
    assert.equal(markers.length, 1);
    assertMatch(markers[0], {
      agent_id: "C",
      event: { subagent_id: "C", workflow_id: "C" },
    });
  });

  for (const [policy, select] of bothPolicies)
    it(`a dead child releases the close gate and the parent completes (${policy})`, async () => {
      const streams = {
        P: [
          ts("P", 1),
          started("P", 1, "C"),
          ms("P", 1, "C", 1),
          rr("P", 1, "C", 1),
          reply("P", 1),
          te("P", 1),
        ],
        C: [],
      };
      const merged = await runMerge(streams, {
        select,
        liveTail: new Set(["C"]),
        stallGraceSeconds: 0.05,
      });
      assert.deepStrictEqual(types(merged.filter((m) => m.agent_id === "P")), types(streams.P));
      const markers = merged.filter((m) => m.event.type === "subagent_stream_unavailable");
      assert.equal(markers.length, 1);
      assertMatch(markers[0]!.event, { subagent_id: "C" });
    });

  it("gives up on a dead child on its own deadline despite a chatty sibling", async () => {
    const dTurn = [
      ts("D", 1),
      ...Array.from({ length: 18 }, (_, i) => tool("D", 1, `d${i}`, true)),
      reply("D", 1),
      te("D", 1),
    ];
    const streams = {
      P: [
        ts("P", 1),
        ms("P", 1, "C", 1),
        ms("P", 1, "D", 1),
        rr("P", 1, "C", 1),
        rr("P", 1, "D", 1),
        reply("P", 1),
        te("P", 1),
      ],
      C: [],
      D: dTurn,
    };
    const merged = await runMerge(streams, {
      select: selectReplay,
      liveTail: new Set(["C"]),
      drip: { D: 0.02 },
      stallGraceSeconds: 0.1,
    });
    assert.equal(types(merged.filter((m) => m.agent_id === "D")).at(-1), "turn_end");
    const markers = merged.flatMap((m, i) =>
      m.event.type === "subagent_stream_unavailable" ? [i] : [],
    );
    assert.equal(markers.length, 1);
    const dTurnEnd = merged.findIndex((m) => m.agent_id === "D" && m.event.type === "turn_end");
    assert.ok(markers[0]! < dTurnEnd);
  });

  it("a child that ends without turn_end releases the gate", async () => {
    const streams = {
      P: [ts("P", 1), ms("P", 1, "C", 1), rr("P", 1, "C", 1), te("P", 1)],
      C: [ts("C", 1), reply("C", 1)],
    };
    const merged = await runMerge(streams, { select: selectReplay });
    assert.deepStrictEqual(types(merged.filter((m) => m.agent_id === "P")), types(streams.P));
    assert.deepStrictEqual(
      types(
        merged.filter((m) => m.agent_id === "C" && m.event.type !== "subagent_stream_unavailable"),
      ),
      ["turn_started", "message_handler_end"],
    );
    assert.equal(merged.filter((m) => m.event.type === "subagent_stream_unavailable").length, 1);
  });

  it("a healthy concurrent child is not given up under a short stall grace", async () => {
    const streams = parentOneChildTurn();
    const merged = await runMerge(streams, { select: selectReplay, stallGraceSeconds: 0.05 });
    assertValidMerge(merged, streams);
    assert.ok(!types(merged).includes("subagent_stream_unavailable"));
  });

  it("an unreadable root ends the merge without raising", async () => {
    const merged = await runMerge(
      { P: [ts("P", 1), reply("P", 1), te("P", 1)] },
      { select: selectReplay, failAfter: { P: 1 } },
    );
    assert.deepStrictEqual(types(merged), ["turn_started"]);
  });

  const threeTurnRoot = () => ({
    P: [1, 2, 3].flatMap((t) => [ts("P", t), reply("P", t), te("P", t)]),
  });

  it("the resume offset advances past each root event", async () => {
    const offsets: number[] = [];
    const merged = await runMerge(threeTurnRoot(), {
      select: selectReplay,
      resumeOffsets: offsets,
    });
    assert.equal(merged.length, 9);
    assert.deepStrictEqual(offsets, [1, 2, 3, 4, 5, 6, 7, 8, 9]);
  });

  it("resuming from an offset streams only the events after it, mid-turn included", async () => {
    const fromTurn = await runMerge(threeTurnRoot(), { select: selectReplay, rootFromOffset: 3 });
    assert.deepStrictEqual(new Set(fromTurn.map((e) => e.turn_number)), new Set([2, 3]));
    assertMatch(fromTurn[0], { turn_number: 2, event: { type: "turn_started" } });
    const midTurn = await runMerge(threeTurnRoot(), { select: selectReplay, rootFromOffset: 4 });
    assert.deepStrictEqual(types(midTurn), [
      "message_handler_end",
      "turn_end",
      "turn_started",
      "message_handler_end",
      "turn_end",
    ]);
    assert.deepStrictEqual(
      midTurn.map((e) => e.turn_number),
      [2, 2, 3, 3, 3],
    );
  });

  const twoTurnsOneChild = () => ({
    P: [
      ts("P", 1),
      ms("P", 1, "C", 1, 0),
      rr("P", 1, "C", 1),
      te("P", 1),
      ts("P", 2),
      ms("P", 2, "C", 2, 3),
      rr("P", 2, "C", 2),
      te("P", 2),
    ],
    C: [ts("C", 1), reply("C", 1), te("C", 1), ts("C", 2), reply("C", 2), te("C", 2)],
  });

  for (const [policy, select] of bothPolicies)
    it(`a resume includes a subagent dispatched at or after the offset (${policy})`, async () => {
      const merged = await runMerge(twoTurnsOneChild(), { select, rootFromOffset: 4 });
      assert.equal(
        merged.every((e) => e.turn_number === 2),
        true,
      );
      const open = types(merged).indexOf("subagent_message_sent");
      const close = types(merged).indexOf("subagent_reply_received");
      const childAt = merged.flatMap((m, i) => (m.agent_id === "C" ? [i] : []));
      assert.ok(childAt.length > 0);
      assert.equal(
        childAt.every((i) => open < i && i < close),
        true,
      );
    });

  for (const [policy, select] of bothPolicies)
    it(`a resume inside a subagent turn omits that subagent but the parent flows (${policy})`, async () => {
      const streams = {
        P: [
          ts("P", 1),
          ms("P", 1, "C", 1, 0),
          tool("P", 1, "t", true),
          rr("P", 1, "C", 1),
          reply("P", 1),
          te("P", 1),
        ],
        C: [ts("C", 1), reply("C", 1), te("C", 1)],
      };
      const merged = await runMerge(streams, { select, rootFromOffset: 2 });
      assert.deepStrictEqual(types(merged.filter((e) => e.agent_id === "P")), [
        "tool_start",
        "subagent_reply_received",
        "message_handler_end",
        "turn_end",
      ]);
      assert.deepStrictEqual(
        merged.filter((m) => m.agent_id === "C"),
        [],
      );
      assert.ok(!types(merged).includes("subagent_stream_unavailable"));
    });

  it("a re-dispatched, given-up child re-enables its close gate", async () => {
    const streams = {
      P: [
        ts("P", 1),
        ms("P", 1, "C", 1, 0),
        rr("P", 1, "C", 1),
        ms("P", 1, "C", 2, 3),
        rr("P", 1, "C", 2),
        te("P", 1),
      ],
      C: [ts("C", 1), reply("C", 1), te("C", 1), ts("C", 2), reply("C", 2), te("C", 2)],
    };
    const merged = await runMerge(streams, { select: selectReplay, rootFromOffset: 2 });
    assert.deepStrictEqual(
      new Set(merged.filter((e) => e.agent_id === "C").map((e) => e.turn_number)),
      new Set([2]),
    );
    const open = types(merged).lastIndexOf("subagent_message_sent");
    const close = types(merged).lastIndexOf("subagent_reply_received");
    const childAt = merged.flatMap((m, i) => (m.agent_id === "C" ? [i] : []));
    assert.ok(childAt.length > 0);
    assert.equal(
      childAt.every((i) => open < i && i < close),
      true,
    );
  });
});

describe("closing the merge", () => {
  const liveRoot = { P: [ts("P", 1), tool("P", 1, "p1", true)] };

  it("an abort ends a merge waiting on a poll, aborting and closing every subscription", async () => {
    const closes: string[] = [];
    const signals: AbortSignal[] = [];
    fakeStreams(liveRoot, { closes, liveTail: new Set(["P"]) });
    const abort = new AbortController();
    const merged = mergeStream({
      client: fakeClient(signals),
      rootWorkflowId: "P",
      rootFromOffset: 0,
      skipUntilTurnId: null,
      select: selectLive,
      shouldStop: async () => false,
      signal: abort.signal,
    });
    assert.equal((await merged.next()).done, false);
    assert.equal((await merged.next()).done, false);
    const waiting = merged.next();
    await sleep(20);
    abort.abort();
    assert.deepStrictEqual(await waiting, { done: true, value: undefined });
    assert.deepStrictEqual(closes, ["P"]);
    assert.ok(signals.length > 0 && signals.every((s) => s.aborted));
  });

  it("returning early closes every subscription", async () => {
    const closes: string[] = [];
    fakeStreams(liveRoot, { closes, liveTail: new Set(["P"]) });
    for await (const _ of mergeStream({
      client: fakeClient(),
      rootWorkflowId: "P",
      rootFromOffset: 0,
      skipUntilTurnId: null,
      select: selectLive,
      shouldStop: async () => false,
    }))
      break;
    assert.deepStrictEqual(closes, ["P"]);
  });
});

describe("subscribe", () => {
  // What the server answers a poll delivered on a workflow task it then failed.
  const taskFailed = () =>
    new ServiceError("Workflow Update failed", {
      cause: Object.assign(
        new Error(
          "9 FAILED_PRECONDITION: Unable to perform workflow execution update due to unexpected workflow task failure.",
        ),
        {
          code: 9,
          details:
            "Unable to perform workflow execution update due to unexpected workflow task failure.",
          metadata: {},
        },
      ),
    });

  it("resubscribes after the last item when a poll's workflow task failed, and rethrows anything else", async () => {
    const froms: number[] = [];
    mock.method(
      WorkflowStreamClient,
      "create",
      () =>
        ({
          subscribe: async function* (_topics: unknown, from: number) {
            froms.push(from);
            if (froms.length === 1) {
              yield { topic: "t", offset: 0, data: "a" };
              yield { topic: "t", offset: 1, data: "b" };
              throw taskFailed();
            }
            if (froms.length === 2) throw taskFailed();
            yield { topic: "t", offset: 2, data: "c" };
            throw new Error("boom");
          },
        }) as unknown as WorkflowStreamClient,
    );
    const seen: unknown[] = [];
    await assert.rejects(
      (async () => {
        for await (const item of subscribe<string>({} as Client, "wf", "t", 0, {}))
          seen.push(item.data);
      })(),
      /boom/,
    );
    assert.deepStrictEqual(seen, ["a", "b", "c"]);
    assert.deepStrictEqual(froms, [0, 2, 2]);
  });

  it("an abort rejects a poll in flight with its reason and return does not wait for it", async () => {
    mock.method(
      WorkflowStreamClient,
      "create",
      () =>
        ({
          subscribe: async function* () {
            await new Promise(() => {});
          },
        }) as unknown as WorkflowStreamClient,
    );
    const abort = new AbortController();
    const items = subscribe<string>(fakeClient(), "wf", "t", 0, { signal: abort.signal });
    const polled = items.next();
    abort.abort(new Error("stop"));
    await assert.rejects(polled, /stop/);
    assert.deepStrictEqual(await items.return(undefined), { done: true, value: undefined });
  });
});
