// What crosses the activity boundary for the Interactions API (Python's _interactions_models.py).
// The request side has no type of its own: the workflow sends the `interactions.create` params as
// they are, plus the turn's stream context.

import type { Interactions } from "@google/genai";

/**
 * The `gemini_interactions_create_streamed` activity's result: every SSE event of the streamed
 * call, verbatim and in order, so the workflow iterates them as it would the SDK's stream.
 */
export interface InteractionResult {
  events: Interactions.InteractionSSEEvent[];
}
