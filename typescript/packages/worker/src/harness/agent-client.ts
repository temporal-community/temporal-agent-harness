// A stateless client for one agent workflow: send messages, resolve approvals and callbacks,
// query status, and read the merged (parent + subagent) event stream. The web API is a thin
// HTTP layer over this.

import {
  WorkflowUpdateFailedError,
  WithStartWorkflowOperation,
  type Client,
} from "@temporalio/client";
import type { Workflow } from "@temporalio/common";
import { WorkflowStreamClient } from "@temporalio/workflow-streams/client";
import {
  AGENT_INTERFACE_QUERY,
  AGENT_STATUS_QUERY,
  PROVIDE_CALLBACK_RESULT_UPDATE,
  SEND_AGENT_MESSAGE_UPDATE,
  TOOL_APPROVAL_UPDATE,
  type AcceptedFunction,
  type AgentConfigInput,
  type AgentEvent,
  type AgentMessageReply,
  type AgentStatus,
  type CallbackResultAck,
  type ToolApprovalResult,
} from "@temporalio/agent-harness/harness/agent-protocol";
import type { z } from "zod";
import type { AgentWorkflow } from "@temporalio/agent-harness";
import type { HandlerDecl } from "@temporalio/agent-harness";
import { mergeStream, selectLive, selectReplay, type Cursor } from "./stream-merge/index.ts";

export {
  mergeStream,
  selectLive,
  selectReplay,
  DEFAULT_STALL_GRACE_SECONDS,
} from "./stream-merge/index.ts";

export const DEFAULT_TURN_TIMEOUT_SECONDS = 300;

export class AgentTurnTimeout extends Error {
  override name = "AgentTurnTimeout";
}
export class AgentTurnError extends Error {
  override name = "AgentTurnError";
}
export class MidTurnRejectedError extends Error {
  override name = "MidTurnRejectedError";
}
export class JoinedTurnError extends Error {
  override name = "JoinedTurnError";
  readonly reply: AgentMessageReply;
  constructor(message: string, reply: AgentMessageReply) {
    super(message);
    this.reply = reply;
  }
}
export class ToolApprovalError extends Error {
  override name = "ToolApprovalError";
  readonly errorType: string | null;
  constructor(message: string, errorType: string | null) {
    super(message);
    this.errorType = errorType;
  }
}
export class CallbackResultError extends Error {
  override name = "CallbackResultError";
  readonly errorType: string | null;
  constructor(message: string, errorType: string | null) {
    super(message);
    this.errorType = errorType;
  }
}

export type AgentStreamOutput = AgentEvent | AgentTurnError | AgentTurnTimeout;

function failureCause(e: unknown): { type: string | null; message: string } | null {
  if (!(e instanceof WorkflowUpdateFailedError)) return null;
  const cause = e.cause as { type?: string; message?: string } | undefined;
  return { type: cause?.type ?? null, message: cause?.message ?? e.message };
}

type Handlers<A> = A extends AgentWorkflow<infer D> ? D["handlers"] : Record<string, HandlerDecl>;
/** A handler name of agent `A`, or any string for an untyped client. */
export type MessageType<A> = Extract<keyof Handlers<A>, string>;
export type MessagePayload<A, K extends MessageType<A>> =
  Handlers<A>[K] extends HandlerDecl<infer I>
    ? z.input<I> & Record<string, unknown>
    : Record<string, unknown>;

/**
 * A client for one agent session. Give it the agent's exported workflow as a type argument
 * (`new AgentClient<typeof MyAgent>(...)`, with `import type`) to type its messages; Python
 * agents use the generated client from `packages/codegen` instead.
 */
export class AgentClient<A = unknown> {
  readonly temporal: Client;
  readonly workflowId: string;
  constructor(temporal: Client, workflowId: string) {
    this.temporal = temporal;
    this.workflowId = workflowId;
  }

  #handle() {
    return this.temporal.workflow.getHandle(this.workflowId);
  }

  getStatus(): Promise<AgentStatus> {
    return this.#handle().query<AgentStatus>(AGENT_STATUS_QUERY);
  }

  getAgentInterface(): Promise<AcceptedFunction[]> {
    return this.#handle().query<AcceptedFunction[]>(AGENT_INTERFACE_QUERY);
  }

  async approveTool(
    toolId: string,
    {
      approved,
      reason = null,
      remember = false,
      updateId,
    }: { approved: boolean; reason?: string | null; remember?: boolean; updateId?: string },
  ): Promise<ToolApprovalResult> {
    try {
      return await this.#handle().executeUpdate<ToolApprovalResult, [unknown]>(
        TOOL_APPROVAL_UPDATE,
        {
          args: [{ tool_id: toolId, approved, reason, remember }],
          ...(updateId ? { updateId } : {}),
        },
      );
    } catch (e) {
      const cause = failureCause(e);
      if (
        cause &&
        (cause.type === "UnknownToolApproval" || cause.type === "ToolApprovalAlreadyResolved")
      ) {
        throw new ToolApprovalError(cause.message, cause.type);
      }
      throw e;
    }
  }

  async provideCallbackResult(
    toolId: string,
    {
      result = null,
      error = null,
      updateId,
    }: { result?: unknown; error?: string | null; updateId?: string },
  ): Promise<CallbackResultAck> {
    try {
      return await this.#handle().executeUpdate<CallbackResultAck, [unknown]>(
        PROVIDE_CALLBACK_RESULT_UPDATE,
        {
          args: [{ tool_id: toolId, result, error }],
          ...(updateId ? { updateId } : {}),
        },
      );
    } catch (e) {
      const cause = failureCause(e);
      if (
        cause &&
        ["UnknownCallback", "CallbackAlreadyResolved", "MalformedCallbackResult"].includes(
          cause.type ?? "",
        )
      ) {
        throw new CallbackResultError(cause.message, cause.type);
      }
      throw e;
    }
  }

  async submitMessage<K extends MessageType<A>>(
    type: K,
    payload: MessagePayload<A, K>,
    { updateId }: { updateId?: string | null } = {},
  ): Promise<AgentMessageReply> {
    try {
      return await this.#handle().executeUpdate<AgentMessageReply, [unknown]>(
        SEND_AGENT_MESSAGE_UPDATE,
        {
          args: [{ type, payload }],
          ...(updateId ? { updateId } : {}),
        },
      );
    } catch (e) {
      const cause = failureCause(e);
      if (cause?.type === "MidTurnRejected") throw new MidTurnRejectedError(cause.message);
      throw e;
    }
  }

  /** Update-with-start: create the session if needed (USE_EXISTING) and deliver the message. */
  async startAndSubmitMessage(
    type: string,
    payload: Record<string, unknown>,
    opts: {
      workflowType: string;
      taskQueue: string;
      startConfig: AgentConfigInput;
      startData?: unknown;
      updateId?: string;
    },
  ): Promise<AgentMessageReply> {
    const startWorkflowOperation = new WithStartWorkflowOperation(opts.workflowType, {
      workflowId: this.workflowId,
      taskQueue: opts.taskQueue,
      args: opts.startData === undefined ? [opts.startConfig] : [opts.startConfig, opts.startData],
      workflowIdConflictPolicy: "USE_EXISTING",
    });
    try {
      return await this.temporal.workflow.executeUpdateWithStart<
        Workflow,
        AgentMessageReply,
        [unknown]
      >(SEND_AGENT_MESSAGE_UPDATE, {
        args: [{ type, payload }],
        startWorkflowOperation,
        ...(opts.updateId ? { updateId: opts.updateId } : {}),
      });
    } catch (e) {
      const cause = failureCause(e);
      if (cause?.type === "MidTurnRejected") throw new MidTurnRejectedError(cause.message);
      throw e;
    }
  }

  /**
   * Send a message and stream its turn (merged with any subagents) until turn_end. Only a
   * message that opens or queues its own turn has a stream; a joined one raises JoinedTurnError.
   */
  async sendMessage<K extends MessageType<A>>(
    type: K,
    payload: MessagePayload<A, K>,
    {
      timeoutSeconds = DEFAULT_TURN_TIMEOUT_SECONDS,
      stallGraceSeconds,
      updateId,
    }: {
      timeoutSeconds?: number | null;
      stallGraceSeconds?: number;
      updateId?: string | null;
    } = {},
  ): Promise<AsyncGenerator<readonly [AgentStreamOutput, number], void, unknown>> {
    const reply = await this.submitMessage(type, payload, { updateId });
    if (reply.disposition === "joined") {
      throw new JoinedTurnError(
        `message ${reply.message_id} joined open turn ${reply.turn_number} and is running; sendMessage cannot stream a joined turn — submitMessage + attach is the mid-turn surface`,
        reply,
      );
    }
    return this.#mergedTurn(reply, timeoutSeconds, stallGraceSeconds);
  }

  async *#mergedTurn(
    reply: AgentMessageReply,
    timeoutSeconds: number | null,
    stallGraceSeconds: number | undefined,
  ) {
    // Aborting ends the merge even while it waits on a poll, so a timeout or an early exit by the
    // consumer always closes its subscriptions.
    const abort = new AbortController();
    const merged = mergeStream({
      client: this.temporal,
      rootWorkflowId: this.workflowId,
      rootFromOffset: reply.accepted_offset,
      skipUntilTurnId: reply.turn_id,
      select: selectLive,
      shouldStop: async (cursor: Cursor, ev: AgentEvent) =>
        !cursor.isChild && ev.turn_id === reply.turn_id && ev.event.type === "turn_end",
      ...(stallGraceSeconds === undefined ? {} : { stallGraceSeconds }),
      signal: abort.signal,
    });
    const deadline = timeoutSeconds === null ? Infinity : Date.now() + timeoutSeconds * 1000;
    const timedOut = Symbol("timeout");
    try {
      while (true) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        const remaining = deadline - Date.now();
        const next = await Promise.race([
          merged.next(),
          ...(Number.isFinite(deadline)
            ? [
                new Promise<typeof timedOut>(
                  (r) => (timer = setTimeout(() => r(timedOut), Math.max(0, remaining))),
                ),
              ]
            : []),
        ]);
        clearTimeout(timer);
        if (next === timedOut) {
          abort.abort();
          await merged.return(undefined);
          yield [
            new AgentTurnTimeout(
              `turn ${reply.turn_number} did not complete within ${timeoutSeconds}s`,
            ),
            -1,
          ] as const;
          return;
        }
        if (next.done) return;
        const [ev, resumeOffset] = next.value;
        if (ev.message_id === reply.message_id && ev.event.type === "message_handler_error") {
          yield [
            new AgentTurnError(ev.event.message || "agent turn failed"),
            resumeOffset,
          ] as const;
        } else {
          yield [ev, resumeOffset] as const;
        }
      }
    } finally {
      abort.abort();
      await merged.return(undefined);
    }
  }

  /**
   * Replay the merged stream from `fromOffset`, then tail it; ends once the agent is idle and
   * every turn it has run is caught up.
   */
  async attach({
    fromOffset = 0,
    stallGraceSeconds,
  }: { fromOffset?: number; stallGraceSeconds?: number } = {}): Promise<
    AsyncGenerator<readonly [AgentEvent, number], void, unknown>
  > {
    const status = await this.getStatus();
    const head = await WorkflowStreamClient.create(this.temporal, this.workflowId).getOffset();
    if (head <= fromOffset && !status.turn_active && status.pending_turns.length === 0) {
      return (async function* () {})();
    }
    let highestCompletedTurn = 0;
    return mergeStream({
      client: this.temporal,
      rootWorkflowId: this.workflowId,
      rootFromOffset: fromOffset,
      skipUntilTurnId: null,
      select: selectReplay,
      ...(stallGraceSeconds === undefined ? {} : { stallGraceSeconds }),
      shouldStop: async (cursor, ev) => {
        if (cursor.isChild) return false;
        const registrationSnapshot = ev.event.type === "state_snapshot" && ev.turn_number === 0;
        if (ev.event.type !== "turn_end" && !registrationSnapshot) return false;
        highestCompletedTurn = Math.max(highestCompletedTurn, ev.turn_number);
        if (cursor.headOffset + 1 < head) return false;
        try {
          const s = await this.getStatus();
          return (
            !s.turn_active && s.pending_turns.length === 0 && highestCompletedTurn >= s.current_turn
          );
        } catch {
          return true;
        }
      },
    });
  }
}
