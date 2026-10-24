// run_subagent_turn: drive one turn on a child agent and return its reply

import { Context, ApplicationFailure } from "@temporalio/activity";
import { WorkflowUpdateFailedError, type Client } from "@temporalio/client";
import { subscribe } from "./stream-merge/index.ts";
import {
  SEND_AGENT_MESSAGE_UPDATE,
  TURN_EVENTS_TOPIC,
  type AgentEvent,
  type AgentMessageReply,
  type RunSubagentTurnInput,
  type SubagentTurnResult,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { publisherFromActivity } from "./agent-workflow.ts";

interface TurnProgress {
  sent: true;
  turn_id: string;
  turn_number: number;
  message_id: string;
  consumed_offset: number;
}

async function submit(client: Client, req: RunSubagentTurnInput): Promise<TurnProgress> {
  const info = Context.current().info;
  const handle = client.workflow.getHandle(req.child_workflow_id);
  try {
    const reply = await handle.executeUpdate<AgentMessageReply, [unknown]>(
      SEND_AGENT_MESSAGE_UPDATE,
      {
        args: [{ type: req.type, payload: req.payload }],
        updateId: `subagent-turn:${info.workflowExecution?.runId}:${info.activityId}`,
      },
    );
    return {
      sent: true,
      turn_id: reply.turn_id,
      turn_number: reply.turn_number,
      message_id: reply.message_id,
      consumed_offset: req.from_offset,
    };
  } catch (e) {
    if (e instanceof WorkflowUpdateFailedError) {
      const cause = e.cause as { type?: string; message?: string } | undefined;
      throw ApplicationFailure.create({
        type: cause?.type ?? "SubagentSendRejected",
        nonRetryable: true,
        message: cause?.message ?? "subagent rejected the message",
      });
    }
    throw e;
  }
}

export async function runSubagentTurn(req: RunSubagentTurnInput): Promise<SubagentTurnResult> {
  const ctx = Context.current();
  const client = ctx.client;
  // A cancellation (from a heartbeat or worker shutdown) ends every wait below and is rethrown.
  let progress = (ctx.info.heartbeatDetails as TurnProgress | undefined) ?? null;
  if (progress === null) {
    progress = await Promise.race([submit(client, req), ctx.cancelled]);
    await using pub = publisherFromActivity(req.parent_stream_context);
    pub.publish({
      type: "subagent_message_sent",
      subagent_id: req.handle,
      agent_key: req.agent_key,
      workflow_id: req.child_workflow_id,
      handler: req.type,
      subagent_turn: progress.turn_number,
      from_offset: req.from_offset,
    });
  }
  ctx.heartbeat(progress);
  const intervalMs = (ctx.info.heartbeatTimeoutMs ?? 20_000) / 2;
  const beat = setInterval(() => ctx.heartbeat(progress), intervalMs);
  let output: Record<string, unknown> = {};
  let gotReply = false;
  try {
    const items = subscribe<AgentEvent>(
      client,
      req.child_workflow_id,
      TURN_EVENTS_TOPIC,
      progress.consumed_offset,
      { pollCooldown: "10 milliseconds", signal: ctx.cancellationSignal },
    );
    for await (const item of items) {
      progress.consumed_offset = item.offset + 1;
      const envelope = item.data;
      if (envelope.turn_id !== progress.turn_id) continue;
      const event = envelope.event;
      const ours = envelope.message_id === progress.message_id;
      if (ours && event.type === "message_handler_error") {
        throw ApplicationFailure.create({
          type: "SubagentTurnError",
          nonRetryable: true,
          message: event.message || "subagent turn failed",
          details: [{ subagent_turn: progress.turn_number }],
        });
      }
      if (ours && event.type === "message_handler_end") {
        output = event.output as Record<string, unknown>;
        gotReply = true;
      }
      if (event.type === "turn_end") break;
    }
  } finally {
    clearInterval(beat);
  }
  if (!gotReply) {
    throw ApplicationFailure.create({
      type: "SubagentNoReply",
      nonRetryable: true,
      message: `subagent turn ${progress.turn_number} ended without a reply`,
      details: [{ subagent_turn: progress.turn_number }],
    });
  }
  return {
    output,
    turn_id: progress.turn_id,
    turn_number: progress.turn_number,
    consumed_offset: progress.consumed_offset,
  };
}
