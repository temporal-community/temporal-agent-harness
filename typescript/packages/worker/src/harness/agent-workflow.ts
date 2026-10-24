import { Context, ApplicationFailure, CancelledFailure } from "@temporalio/activity";
import { WorkflowStreamClient } from "@temporalio/workflow-streams/client";
import type { Duration } from "@temporalio/common";
import type { AgentToolContext, HarnessTool } from "@temporalio/agent-harness";
import { toolOutputText } from "@temporalio/agent-harness";
import {
  TURN_EVENTS_TOPIC,
  type AgentEvent,
  type AgentStreamItem,
  type TurnStreamContext,
  validate,
} from "@temporalio/agent-harness/harness/agent-protocol";

/** Each flush is one signal into the workflow: UI snappiness traded against history volume. */
export const DEFAULT_PUBLISH_BATCH_INTERVAL: Duration = "50 milliseconds";

/**
 * Heartbeat the current activity every 5 seconds until the returned function is called, so a
 * model call that waits on a silent provider still shows its worker is alive (the activity SDK
 * throttles the RPCs; the integrations' heartbeat timeout is a minute).
 */
export function heartbeatEvery5s(): () => void {
  const ctx = Context.current();
  const timer = setInterval(() => ctx.heartbeat(), 5_000);
  return () => clearInterval(timer);
}

export interface TurnEventPublisher extends AsyncDisposable {
  publish(event: AgentStreamItem): void;
  flush(): Promise<void>;
}

/** Publish envelopes for `context`'s turn from inside an activity: `await using pub = ...`. */
export function publisherFromActivity(
  context: TurnStreamContext,
  { batchInterval = DEFAULT_PUBLISH_BATCH_INTERVAL }: { batchInterval?: Duration } = {},
): TurnEventPublisher {
  const client = WorkflowStreamClient.fromWithinActivity({ batchInterval });
  const topic = client.topic<AgentEvent>(TURN_EVENTS_TOPIC);
  return {
    publish(event) {
      topic.publish({
        agent_id: context.agent_id,
        turn_id: context.turn_id,
        turn_number: context.turn_number,
        message_id: context.message_id,
        timestamp: Date.now() / 1000,
        event,
      });
    },
    flush: () => client.flush(),
    [Symbol.asyncDispose]: () => client[Symbol.asyncDispose](),
  };
}

type ActivityToolTypes<T> = T extends {
  readonly __activity?: { input: infer I; output: infer O; injected: infer J };
}
  ? { input: I; output: O; injected: J }
  : never;

/** What a `toolActivity` body gets besides its input: the call's tool_id and a publisher on its turn. */
export interface ToolActivityContext extends AgentToolContext {
  publish: TurnEventPublisher["publish"];
}

/**
 * The activity body of an `activityTool` tool, keyed by the tool's name for a worker's
 * `activities`. Publishes tool_start and then tool_end or tool_error from the activity itself, on
 * every attempt, so tool_start marks when an attempt actually began executing on a worker. A
 * cancelled attempt (its timeout included) publishes neither: it ends at the cancellation, as
 * Python's does. The body's third argument carries the call's `tool_id` (key idempotent side
 * effects on it) and `publish` (e.g. for `tool_progress_delta`).
 */
export function toolActivity<
  T extends HarnessTool<never, unknown> & { readonly __activity?: unknown },
>(
  tool: T,
  impl: (
    input: ActivityToolTypes<T>["input"],
    injected: ActivityToolTypes<T>["injected"],
    ctx: ToolActivityContext,
  ) => Promise<ActivityToolTypes<T>["output"]>,
): Record<string, (input: unknown, injected: unknown, ctx: AgentToolContext) => Promise<unknown>> {
  if (tool.kind !== "activity")
    throw new TypeError(
      `${tool.name} is not an activityTool tool, so it has no activity to register`,
    );
  return {
    async [tool.name](input: unknown, injected: unknown, ctx: AgentToolContext) {
      const parsed = validate(tool.inputSchema, input);
      if (!parsed.ok)
        throw ApplicationFailure.create({
          type: "InvalidToolInput",
          nonRetryable: true,
          message: `Payload for tool '${tool.name}' does not match its schema: ${parsed.message}`,
        });
      await using pub = publisherFromActivity(ctx.stream_context);
      pub.publish({
        type: "tool_start",
        tool_id: ctx.tool_id,
        tool_name: tool.name,
        tool_input: input as Record<string, unknown>,
      });
      const toolCtx: ToolActivityContext = {
        stream_context: ctx.stream_context,
        tool_id: ctx.tool_id,
        publish: (event) => pub.publish(event),
      };
      const activity = Context.current();
      let result: ActivityToolTypes<T>["output"];
      try {
        result = await heartbeating(() =>
          Promise.race([
            impl(
              parsed.value as ActivityToolTypes<T>["input"],
              (injected ?? {}) as ActivityToolTypes<T>["injected"],
              toolCtx,
            ),
            activity.cancelled,
          ]),
        );
      } catch (e) {
        if (!(e instanceof CancelledFailure) && !activity.cancellationSignal.aborted)
          pub.publish({
            type: "tool_error",
            tool_id: ctx.tool_id,
            tool_name: tool.name,
            message: e instanceof Error ? e.message : String(e),
          });
        throw e;
      }
      pub.publish({
        type: "tool_end",
        tool_id: ctx.tool_id,
        tool_name: tool.name,
        tool_output: toolOutputText(result),
      });
      return result;
    },
  };
}

/**
 * Run `work`, heartbeating at half the activity's heartbeat timeout, so a worker that dies is
 * noticed then rather than at the start-to-close timeout, and a cancellation reaches the activity.
 */
export async function heartbeating<T>(work: () => Promise<T>): Promise<T> {
  const ctx = Context.current();
  const timeout = ctx.info.heartbeatTimeoutMs;
  const beat = timeout ? setInterval(() => ctx.heartbeat(), timeout / 2) : undefined;
  try {
    return await work();
  } finally {
    clearInterval(beat);
  }
}

/** Merge several `toolActivity(...)` registrations into one activities object. */
export const toolActivities = (
  ...registrations: Record<string, unknown>[]
): Record<string, unknown> => Object.assign({}, ...registrations);

/** A failure a tool body throws when retrying cannot help: a bad input, a missing key, a policy refusal. */
export function nonRetryable(type: string, message: string): ApplicationFailure {
  return ApplicationFailure.create({ type, message, nonRetryable: true });
}
