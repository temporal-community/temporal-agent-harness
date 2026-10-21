import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { HarnessStateError } from "@temporalio/agent-harness";
import type { AgentEvent, StreamEventOf } from "@temporalio/agent-harness/harness/agent-protocol";
import { AgentClient } from "../../../src/harness/agent-client.ts";
import { assertMatch } from "../../match.ts";
import { collect, send, startEnv, type TestEnv } from "../../support.ts";
import { harness, replay, type StateEvent, type StateSnapshotEvent } from "./state-support.ts";

describe("StateRef", () => {
  it("starts at version 0 with a snapshot of the schema's defaults", () => {
    const { ref, published } = harness();
    assert.deepStrictEqual(ref.snapshot(), {
      type: "state_snapshot",
      state_id: "plan",
      version: 0,
      value: { goal: "", steps: [], scratch: {} },
    });
    assert.equal(published.length, 0);
  });

  it("publishes one patch per committing mutate, and replaying the ops reproduces the value", () => {
    const { ref, published } = harness();
    const start = ref.snapshot().value;
    ref.mutate((d) => {
      d.goal = "ship";
      d.steps.push({ name: "research", done: false }, { name: "write", done: false });
    });
    ref.mutate((d) => {
      d.steps[0]!.done = true;
      d.scratch.note = "research finished";
    });
    ref.mutate((d) => {
      d.steps.splice(0, 1);
    });
    assert.deepStrictEqual(
      published.map((e) => e.version),
      [1, 2, 3],
    );
    assert.equal(ref.version, 3);
    const patches = published as Extract<StateEvent, { type: "state_patch" }>[];
    const byPath = (ops: Record<string, unknown>[]) =>
      [...ops].sort((a, b) => String(a.path).localeCompare(String(b.path)));
    assert.deepStrictEqual(byPath(patches[0]!.ops), [
      { op: "replace", path: "/goal", value: "ship" },
      { op: "add", path: "/steps/-", value: { name: "research", done: false } },
      { op: "add", path: "/steps/-", value: { name: "write", done: false } },
    ]);
    assert.deepStrictEqual(byPath(patches[1]!.ops), [
      { op: "add", path: "/scratch/note", value: "research finished" },
      { op: "replace", path: "/steps/0/done", value: true },
    ]);
    assert.equal(
      patches
        .flatMap((p) => p.ops)
        .every((op) => ["add", "replace", "remove"].includes(op.op as string)),
      true,
    );
    assert.deepStrictEqual(replay(start, patches), {
      goal: "ship",
      steps: [{ name: "write", done: false }],
      scratch: { note: "research finished" },
    });
    assert.deepStrictEqual(replay(start, patches), ref.current);
  });

  it("a no-op mutate publishes nothing and keeps the version", () => {
    const { ref, published } = harness();
    ref.mutate(() => {});
    ref.mutate((d) => {
      d.goal = "";
    });
    assert.equal(published.length, 0);
    assert.equal(ref.version, 0);
  });

  it("an exception inside mutate rolls everything back", () => {
    const { ref, published } = harness();
    assert.throws(
      () =>
        ref.mutate((d) => {
          d.goal = "half done";
          throw new Error("boom");
        }),
      /boom/,
    );
    assert.equal(ref.current.goal, "");
    assert.equal(published.length, 0);
    ref.mutate((d) => {
      d.goal = "still usable";
    });
    assert.equal(ref.version, 1);
  });

  it("revokes the draft when mutate returns", () => {
    const { ref } = harness();
    let leaked: { goal: string } | undefined;
    ref.mutate((d) => {
      leaked = d;
      d.goal = "x";
    });
    assert.throws(() => leaked!.goal, TypeError);
  });

  it("rejects a nested mutate, and a set inside a mutate, without corrupting the stream", () => {
    const { ref, published } = harness();
    assert.throws(() => ref.mutate(() => ref.mutate(() => {})), HarnessStateError);
    assert.throws(
      () =>
        ref.mutate((d) => {
          d.goal = "from the draft";
          ref.set({ goal: "from set" });
        }),
      HarnessStateError,
    );
    assert.equal(published.length, 0);
    assert.equal(ref.current.goal, "");
  });

  it("rejects an async recipe", () => {
    const { ref } = harness();
    assert.throws(() => ref.mutate((async () => {}) as never), /must be synchronous/);
  });

  it("set() replaces the root with one op at the empty pointer", () => {
    const { ref, published } = harness();
    ref.set({ goal: "fresh" });
    assert.deepStrictEqual(published, [
      {
        type: "state_patch",
        state_id: "plan",
        version: 1,
        ops: [{ op: "replace", path: "", value: { goal: "fresh", steps: [], scratch: {} } }],
      },
    ]);
    assert.deepStrictEqual(ref.current, { goal: "fresh", steps: [], scratch: {} });
  });

  it("validates every commit against the schema, leaving current and the stream untouched on failure", () => {
    const { ref, published } = harness();
    assert.throws(
      () =>
        ref.mutate((d) => {
          (d as { goal: unknown }).goal = 42;
        }),
      HarnessStateError,
    );
    assert.throws(() => ref.set({ goal: 7 } as never));
    assert.equal(ref.current.goal, "");
    assert.equal(ref.version, 0);
    assert.equal(published.length, 0);
    ref.mutate((d) => {
      d.goal = "ok";
    });
    assert.deepStrictEqual(
      published.map((e) => e.version),
      [1],
    );
    assert.deepStrictEqual(replay(ref.snapshot().value, []), ref.current);
  });

  it("an initial value that does not fit the schema fails at registration", () => {
    assert.throws(() => harness(() => ({ goal: 3 }) as never));
  });

  it("reads are deeply frozen", () => {
    const { ref } = harness();
    ref.mutate((d) => {
      d.steps.push({ name: "a", done: false });
    });
    assert.equal(Object.isFrozen(ref.current), true);
    assert.equal(Object.isFrozen(ref.current.steps), true);
    assert.equal(Object.isFrozen(ref.current.steps[0]), true);
    assert.throws(() => {
      (ref.current as { goal: string }).goal = "x";
    }, TypeError);
    assert.throws(() => (ref.current.steps as unknown[]).push({}), TypeError);
  });

  it("shares untouched subtrees by identity, and old values survive later commits", () => {
    const { ref } = harness(() => ({ steps: [{ name: "a" }], scratch: { k: "v" } }));
    const before = ref.current;
    ref.mutate((d) => {
      d.goal = "changed";
    });
    assert.equal(ref.current.steps, before.steps);
    assert.equal(ref.current.scratch, before.scratch);
    assert.equal(before.goal, "");
  });

  it("escapes RFC 6901 pointer segments", () => {
    const { ref, published } = harness();
    ref.mutate((d) => {
      d.scratch["a/b~c"] = "x";
    });
    assert.deepStrictEqual((published[0] as { ops: unknown[] }).ops, [
      { op: "add", path: "/scratch/a~1b~0c", value: "x" },
    ]);
  });

  it("snapshots are detached copies", () => {
    const { ref } = harness();
    const snap: StateSnapshotEvent = ref.snapshot();
    (snap.value as { goal: string }).goal = "tampered";
    assert.equal(ref.current.goal, "");
  });
});

describe("observable state on a running agent", () => {
  let env: TestEnv;
  before(async () => {
    env = await startEnv("state");
  });
  after(async () => {
    await env.stop();
  });

  async function runTurn(goal = "ship the feature"): Promise<AgentEvent[]> {
    const handle = await env.start("StateProbeAgent");
    await send(handle, "make_plan", { text: goal });
    return collect(env.client, handle.workflowId);
  }
  const ofType = <T extends "state_snapshot" | "state_patch">(events: AgentEvent[], type: T) =>
    events.filter((e) => e.event.type === type) as (AgentEvent & { event: StreamEventOf<T> })[];

  it("publishes the registration snapshot outside any turn, then one patch per commit stamped with its turn", async () => {
    const events = await runTurn();
    const snapshots = ofType(events, "state_snapshot");
    assert.equal(snapshots.length, 1);
    assert.equal(events.indexOf(snapshots[0]!), 0);
    assertMatch(snapshots[0], {
      turn_number: 0,
      event: { state_id: "plan", version: 0, value: { goal: "", steps: [], scratch: {} } },
    });

    const patches = ofType(events, "state_patch");
    assert.deepStrictEqual(
      patches.map((p) => p.event.version),
      [1, 2, 3],
    );
    const turnId = events.find((e) => e.event.type === "turn_started")!.turn_id;
    assert.equal(
      patches.every((p) => p.turn_id === turnId && p.turn_number === 1 && p.agent_id),
      true,
    );
    const kinds = events.map((e) => e.event.type);
    assert.ok(kinds.indexOf("turn_started") < kinds.indexOf("state_patch"));
    assert.ok(kinds.indexOf("state_patch") < kinds.indexOf("turn_end"));

    assert.deepStrictEqual(
      replay(
        snapshots[0]!.event.value,
        patches.map((p) => p.event),
      ),
      {
        goal: "ship the feature",
        steps: [{ name: "write", done: false }],
        scratch: { note: "research finished" },
      },
    );
  });

  it("two workflows of one type on one worker never share state", async () => {
    const [first, second] = await Promise.all([runTurn("first goal"), runTurn("second goal")]);
    for (const [events, goal] of [
      [first, "first goal"],
      [second, "second goal"],
    ] as const) {
      const [snapshot] = ofType(events, "state_snapshot");
      assertMatch(snapshot!.event.value, { goal: "" });
      assertMatch(
        replay(
          snapshot!.event.value,
          ofType(events, "state_patch").map((p) => p.event),
        ),
        { goal },
      );
    }
  });

  it("attaching to a brand-new session delivers the snapshot and stops", async () => {
    const handle = await env.start("StateProbeAgent");
    const items: AgentEvent[] = [];
    for await (const [item] of await new AgentClient(env.client, handle.workflowId).attach({
      fromOffset: 0,
    }))
      items.push(item);
    assert.deepStrictEqual(
      items.map((i) => i.event.type),
      ["state_snapshot"],
    );
    assertMatch(items[0], { turn_number: 0, event: { version: 0 } });
  });
});
