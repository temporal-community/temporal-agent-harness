// run_subagent_turn reads only its own reply from a shared child turn, against a faked stream.

import assert from "node:assert/strict";
import { afterEach, describe, it, mock } from "node:test";
import { ApplicationFailure } from "@temporalio/common";
import { WorkflowUpdateFailedError, type Client } from "@temporalio/client";
import { MockActivityEnvironment } from "@temporalio/testing";
import { WorkflowStreamClient } from "@temporalio/workflow-streams/client";
import type {
  AgentEvent,
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

  for (const [name, type, nonRetryable] of [
    ["an update the worker never processed is retried", "UnprocessedUpdate", false],
    ["a rejected update fails the tool call", "SomeRejection", true],
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
      assert.equal((err as ApplicationFailure).nonRetryable ?? false, nonRetryable);
    });
});
