// AgentClient closes the merged stream behind sendMessage and attach when a consumer stops early
// or the turn times out, against a faked client and streams.

import assert from "node:assert/strict";
import { afterEach, describe, it, mock } from "node:test";
import { setTimeout as sleep } from "node:timers/promises";
import type { Client } from "@temporalio/client";
import { WorkflowStreamClient } from "@temporalio/workflow-streams/client";
import type {
  AgentEvent,
  AgentMessageReply,
  AgentStatus,
  AgentStreamItem,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { AgentClient, AgentTurnTimeout } from "../../src/harness/agent-client.ts";

const ev = (event: AgentStreamItem): AgentEvent => ({
  agent_id: "P",
  turn_id: "P-t1",
  turn_number: 1,
  message_id: null,
  timestamp: 0,
  event,
});

/** P's turn is still running: its stream never reaches turn_end and its next poll never answers. */
const liveTurn = [
  ev({ type: "turn_started" }),
  ev({ type: "tool_start", tool_id: "t1", tool_name: "x", tool_input: {} }),
];

/** Fakes P's stream as `liveTurn`, recording each close of a subscription to it. */
function liveStream(closes: string[]) {
  mock.method(
    WorkflowStreamClient,
    "create",
    (_client: Client, workflowId: string) =>
      ({
        getOffset: async () => liveTurn.length,
        subscribe: (_topics: unknown, fromOffset: number) => {
          const inner = (async function* () {
            for (let offset = fromOffset; offset < liveTurn.length; offset++)
              yield { topic: "turn_events", data: liveTurn[offset]!, offset };
            await new Promise(() => {});
          })();
          return {
            next: () => inner.next(),
            return: (v: undefined) => {
              closes.push(workflowId);
              return inner.return(v);
            },
            [Symbol.asyncIterator]() {
              return this;
            },
          };
        },
      }) as unknown as WorkflowStreamClient,
  );
}

const reply: AgentMessageReply = {
  turn_number: 1,
  turn_id: "P-t1",
  message_id: "m1",
  disposition: "opened",
  accepted_offset: 0,
};

/** A client whose calls cannot be aborted, recording each abort signal it is handed. */
function fakeClient(signals: AbortSignal[] = []): Client {
  const status: Partial<AgentStatus> = { turn_active: true, pending_turns: [], current_turn: 1 };
  return {
    withAbortSignal: (signal: AbortSignal, fn: () => unknown) => {
      signals.push(signal);
      return fn();
    },
    workflow: {
      getHandle: () => ({
        executeUpdate: async () => reply,
        query: async () => status,
      }),
    },
  } as unknown as Client;
}

afterEach(() => {
  mock.restoreAll();
});

describe("AgentClient closes its merged stream", () => {
  it("breaking out of sendMessage's stream closes the merge's subscriptions", async () => {
    const closes: string[] = [];
    liveStream(closes);
    const client = new AgentClient(fakeClient(), "P");
    for await (const [item] of await client.sendMessage("ask", {}, { timeoutSeconds: null })) {
      assert.ok(!(item instanceof Error));
      assert.equal(item.event.type, "turn_started");
      break;
    }
    assert.deepStrictEqual(closes, ["P"]);
  });

  it("a timeout yields AgentTurnTimeout last and closes the merge without waiting on its poll", async () => {
    const closes: string[] = [];
    const signals: AbortSignal[] = [];
    liveStream(closes);
    const client = new AgentClient(fakeClient(signals), "P");
    const items: unknown[] = [];
    const drained = (async () => {
      for await (const [item] of await client.sendMessage("ask", {}, { timeoutSeconds: 0.1 }))
        items.push(item);
    })();
    await Promise.race([
      drained,
      sleep(2000).then(() => assert.fail("the timed-out stream did not close")),
    ]);
    assert.equal(items.length, liveTurn.length + 1);
    assert.ok(items.at(-1) instanceof AgentTurnTimeout);
    assert.deepStrictEqual(closes, ["P"]);
    assert.ok(signals.length > 0 && signals.every((s) => s.aborted));
  });

  it("breaking out of attach's stream closes the merge's subscriptions", async () => {
    const closes: string[] = [];
    liveStream(closes);
    const client = new AgentClient(fakeClient(), "P");
    for await (const [item] of await client.attach()) {
      assert.equal(item.event.type, "turn_started");
      break;
    }
    assert.deepStrictEqual(closes, ["P"]);
  });
});
