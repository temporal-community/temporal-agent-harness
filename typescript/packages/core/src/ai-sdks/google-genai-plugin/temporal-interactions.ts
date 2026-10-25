// The workflow-side `interactions` module (Python's _temporal_interactions.py): `create` has the
// SDK's shape but runs the call in the `gemini_interactions_create_streamed` activity, which holds
// the worker's real client and publishes the call's turn events as it streams.

import type { Interactions } from "@google/genai";
import { proxyActivities, type ActivityOptions } from "@temporalio/workflow";
import type { TurnStreamContext } from "../../harness/agent-protocol/index.ts";
import type { AgentWorkflowRunner } from "../../harness/agent-workflow.ts";
import type { InteractionResult } from "./interactions-models.ts";

export const GEMINI_INTERACTIONS_CREATE_STREAMED_ACTIVITY = "gemini_interactions_create_streamed";

/** The params of a streamed `interactions.create`, passed to the worker's client as they are. */
export type InteractionCreateParamsStreaming =
  Interactions.InteractionCreateParams.InteractionCreateParamsStreaming;

export type GeminiInteractionsActivity = (
  params: InteractionCreateParamsStreaming,
  streamContext: TurnStreamContext | null,
) => Promise<InteractionResult>;

/**
 * The events of one streamed call, already collected by the activity. Iterate it with
 * `for await`, as the SDK's stream; each iteration walks the same events again.
 */
export class TemporalAsyncInteractionStream implements AsyncIterable<Interactions.InteractionSSEEvent> {
  readonly #events: readonly Interactions.InteractionSSEEvent[];

  constructor(events: readonly Interactions.InteractionSSEEvent[]) {
    this.#events = events;
  }

  async *[Symbol.asyncIterator](): AsyncIterator<Interactions.InteractionSSEEvent> {
    yield* this.#events;
  }
}

/**
 * The client's `interactions`. Only the streaming `create` is supported; it runs in an activity
 * whose options default to a 3 minute start-to-close timeout.
 */
export class TemporalAsyncInteractions {
  readonly #runner: AgentWorkflowRunner | null;
  readonly #activityConfig: ActivityOptions;

  constructor(runner: AgentWorkflowRunner | null, activityConfig?: ActivityOptions) {
    this.#runner = runner;
    this.#activityConfig = activityConfig ?? { startToCloseTimeout: "3 minutes" };
  }

  /**
   * One streamed `interactions.create`. With a runner, the activity publishes the call's events
   * (model_interaction_started, reply_delta, tool_requested, ...) to the runner's current turn.
   */
  async create(params: InteractionCreateParamsStreaming): Promise<TemporalAsyncInteractionStream> {
    if (params.stream !== true)
      throw new TypeError(
        "TemporalAsyncInteractions only supports stream: true; pass stream: true or extend the " +
          "shim for the non-streaming path.",
      );
    const streamContext = this.#runner === null ? null : this.#runner.currentStreamContext;
    const activities = proxyActivities<
      Record<typeof GEMINI_INTERACTIONS_CREATE_STREAMED_ACTIVITY, GeminiInteractionsActivity>
    >({ summary: "interactions.create", ...this.#activityConfig });
    const result = await activities[GEMINI_INTERACTIONS_CREATE_STREAMED_ACTIVITY](
      params,
      streamContext,
    );
    return new TemporalAsyncInteractionStream(result.events);
  }
}
