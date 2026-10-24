// run_subagent_turn reads only its own reply from a shared child turn, sends idempotently and
// stops on cancellation, against a faked client and stream.

import assert from "node:assert/strict";
import { afterEach, describe, it, mock } from "node:test";
import { setTimeout as sleep } from "node:timers/promises";
import { ApplicationFailure, CancelledFailure } from "@temporalio/common";
import { WorkflowUpdateFailedError, type Client } from "@temporalio/client";
import { MockActivityEnvironment } from "@temporalio/testing";
import { WorkflowStreamClient } from "@temporalio/workflow-streams/client";
import type {
  AgentEvent,
  AgentMessageReply,
  AgentStreamItem,
  RunSubagentTurnInput,
  SubagentTurnResult,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { runSubagentTurn } from "../../../src/harness/subagent-activities.ts";
import { rejection } from "./helpers.ts";

describe("run_subagent_turn reads only its own reply from a shared child turn", () => {
  afterEach(() => {
    mock.restoreAll();
  });
  const childEvent = (event: AgentStreamItem, messageId: string | null): AgentEvent => ({
    agent_id: "child",
    turn_id: "child-turn-4",
    turn_number: 4,
    message_id: messageId,
    timestamp: 0,
    event,
  });
  function consume(events: AgentEvent[]): Promise<SubagentTurnResult> {
    mock.method(
      WorkflowStreamClient,
      "create",
      () =>
        ({
          subscribe: async function* () {
            for (const [offset, data] of events.entries())
              yield { topic: "turn_events", offset, data };
          },
        }) as unknown as WorkflowStreamClient,
    );
    const req: RunSubagentTurnInput = {
      child_workflow_id: "child-wf",
      type: "ask",
      payload: {},
      from_offset: 0,
      handle: "aaaaaa-bbbbbb",
      agent_key: "sample",
      parent_stream_context: {
        turn_id: "parent-turn-1",
        turn_number: 1,
        message_id: null,
        agent_id: "aaaaaa",
      },
    };
    const progress = {
      sent: true,
      turn_id: "child-turn-4",
      turn_number: 4,
      message_id: "ours",
      consumed_offset: 0,
    };
    const client = {
      withAbortSignal: (_signal: AbortSignal, fn: () => unknown) => fn(),
    } as unknown as Client;
    return new MockActivityEnvironment(
      { heartbeatDetails: progress, heartbeatTimeoutMs: 20_000 },
      { client },
    ).run(runSubagentTurn, req);
  }
  const end = (text: string, id: string) =>
    childEvent({ type: "message_handler_end", output: { text } }, id);
  const turnEnd = childEvent({ type: "turn_end" }, null);

  it("returns our reply, not the last one", async () => {
    assert.deepStrictEqual(
      (await consume([end("ours", "ours"), end("theirs", "theirs"), turnEnd])).output,
      { text: "ours" },
    );
  });

  it("another participant's error is not our tool call failing", async () => {
    const theirError = childEvent(
      { type: "message_handler_error", message: "their handler blew up" },
      "theirs",
    );
    assert.deepStrictEqual((await consume([theirError, end("ours", "ours"), turnEnd])).output, {
      text: "ours",
    });
  });

  it("our own error fails the tool call", async () => {
    const err = await rejection(
      consume([
        childEvent({ type: "message_handler_error", message: "ours blew up" }, "ours"),
        turnEnd,
      ]),
    );
    assert.ok(err instanceof ApplicationFailure);
    assert.equal(err.type, "SubagentTurnError");
    assert.match(err.message, /ours blew up/);
  });

  it("a turn_end with no reply for us reports no reply", async () => {
    const err = await rejection(consume([end("theirs", "theirs"), turnEnd]));
    assert.ok(err instanceof ApplicationFailure);
    assert.equal(err.type, "SubagentNoReply");
  });

  for (const [name, type] of [
    ["an update the worker never processed fails the tool call", "UnprocessedUpdate"],
    ["a rejected update fails the tool call", "SomeRejection"],
  ] as const)
    it(name, async () => {
      const failure = new WorkflowUpdateFailedError(
        "update failed",
        ApplicationFailure.create({ type, message: "no" }),
      );
      const client = {
        withAbortSignal: (_signal: AbortSignal, fn: () => unknown) => fn(),
        workflow: { getHandle: () => ({ executeUpdate: () => Promise.reject(failure) }) },
      } as unknown as Client;
      const req = {
        child_workflow_id: "child-wf",
        type: "ask",
        payload: {},
        from_offset: 0,
      } as unknown as RunSubagentTurnInput;
      const err = await rejection(
        new MockActivityEnvironment({}, { client }).run(runSubagentTurn, req),
      );
      assert.ok(err instanceof ApplicationFailure);
      assert.equal(err.type, type);
      assert.equal(err.nonRetryable, true);
    });
});

describe("run_subagent_turn sends idempotently and stops on cancellation", () => {
  afterEach(() => {
    mock.restoreAll();
  });
  const req: RunSubagentTurnInput = {
    child_workflow_id: "child-wf",
    type: "ask",
    payload: {},
    from_offset: 0,
    handle: "aaaaaa-bbbbbb",
    agent_key: "sample",
    parent_stream_context: {
      turn_id: "parent-turn-1",
      turn_number: 1,
      message_id: null,
      agent_id: "aaaaaa",
    },
  };
  const childEvent = (event: AgentStreamItem, messageId: string | null): AgentEvent => ({
    agent_id: "child",
    turn_id: "child-turn-4",
    turn_number: 4,
    message_id: messageId,
    timestamp: 0,
    event,
  });

  /** A child stream that yields `events` and then, like a live idle workflow, never answers. */
  function childStream(events: AgentEvent[], closes: string[] = []) {
    mock.method(
      WorkflowStreamClient,
      "create",
      () =>
        ({
          subscribe: () => {
            const inner = (async function* () {
              for (const [offset, data] of events.entries())
                yield { topic: "turn_events", offset, data };
              await new Promise(() => {});
            })();
            return {
              next: () => inner.next(),
              return: (v: undefined) => {
                closes.push("child-wf");
                return inner.return(v);
              },
              [Symbol.asyncIterator]() {
                return this;
              },
            };
          },
        }) as unknown as WorkflowStreamClient,
    );
    mock.method(
      WorkflowStreamClient,
      "fromWithinActivity",
      () =>
        ({
          topic: () => ({ publish: () => undefined }),
          flush: async () => undefined,
          [Symbol.asyncDispose]: async () => undefined,
        }) as unknown as WorkflowStreamClient,
    );
  }

  it("sends with the same update id on every attempt, so a re-send after a crash is deduplicated", async () => {
    childStream([
      childEvent({ type: "message_handler_end", output: { text: "hi" } }, "ours"),
      childEvent({ type: "turn_end" }, null),
    ]);
    const updateIds: unknown[] = [];
    const accepted: AgentMessageReply = {
      turn_number: 4,
      turn_id: "child-turn-4",
      message_id: "ours",
      disposition: "opened",
      accepted_offset: 0,
    };
    const client = {
      withAbortSignal: (_signal: AbortSignal, fn: () => unknown) => fn(),
      workflow: {
        getHandle: () => ({
          executeUpdate: async (_name: string, opts: { updateId?: string }) => {
            updateIds.push(opts.updateId);
            return accepted;
          },
        }),
      },
    } as unknown as Client;
    const workflowExecution = { workflowId: "parent-wf", runId: "parent-run" };
    for (const attempt of [1, 2]) {
      const result: SubagentTurnResult = await new MockActivityEnvironment(
        { attempt, activityId: "7", workflowExecution, heartbeatTimeoutMs: 20_000 },
        { client },
      ).run(runSubagentTurn, req);
      assert.deepStrictEqual(result.output, { text: "hi" });
    }
    assert.deepStrictEqual(updateIds, [
      "subagent-turn:parent-run:7",
      "subagent-turn:parent-run:7",
    ]);
  });

  it("a cancel while waiting on the child's stream stops polling and heartbeating and rethrows", async () => {
    const closes: string[] = [];
    childStream([childEvent({ type: "turn_started" }, null)], closes);
    const client = {
      withAbortSignal: (_signal: AbortSignal, fn: () => unknown) => fn(),
    } as unknown as Client;
    const progress = {
      sent: true,
      turn_id: "child-turn-4",
      turn_number: 4,
      message_id: "ours",
      consumed_offset: 0,
    };
    const env = new MockActivityEnvironment(
      { heartbeatDetails: progress, heartbeatTimeoutMs: 40 },
      { client },
    );
    let beats = 0;
    env.on("heartbeat", () => beats++);
    const running = env.run(runSubagentTurn, req);
    await sleep(100);
    assert.ok(beats > 1, "heartbeats while waiting on the child");
    env.cancel();
    const err = await Promise.race([
      rejection(running),
      sleep(1000).then(() => assert.fail("the activity did not stop on cancellation")),
    ]);
    assert.ok(err instanceof CancelledFailure);
    assert.deepStrictEqual(closes, ["child-wf"]);
    const beatsAtCancel = beats;
    await sleep(100);
    assert.equal(beats, beatsAtCancel);
  });

  it("a cancel while the send is in flight rethrows without waiting for the send", async () => {
    childStream([]);
    const client = {
      withAbortSignal: (_signal: AbortSignal, fn: () => unknown) => fn(),
      workflow: { getHandle: () => ({ executeUpdate: () => new Promise(() => {}) }) },
    } as unknown as Client;
    const env = new MockActivityEnvironment({ heartbeatTimeoutMs: 40 }, { client });
    const running = env.run(runSubagentTurn, req);
    await sleep(20);
    env.cancel();
    const err = await Promise.race([
      rejection(running),
      sleep(1000).then(() => assert.fail("the activity did not stop on cancellation")),
    ]);
    assert.ok(err instanceof CancelledFailure);
  });
});
