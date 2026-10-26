// Workflow utilities for the Gemini integration (Python's google_genai_plugin/workflow.py).

import type { ActivityOptions } from "@temporalio/workflow";
import type { AgentWorkflowRunner } from "../../harness/agent-workflow.ts";
import { TemporalAsyncClient } from "./temporal-async-client.ts";

/**
 * A Gemini client for workflow code whose API calls run as activities on the worker's
 * `GoogleGenAIPlugin` client, so credentials never enter the workflow or its history.
 *
 * `activityConfig` replaces the activity options (default: a 3 minute start-to-close timeout).
 * With `runner`, a streamed call publishes its events (model_interaction_started, reply_delta,
 * tool_requested, model_interaction_ended, ...) to the runner's current turn from the activity;
 * without it, streaming still works but nothing reaches the turn's stream.
 */
export function googleGenaiClient({
  activityConfig,
  runner = null,
}: {
  activityConfig?: ActivityOptions;
  runner?: AgentWorkflowRunner | null;
} = {}): TemporalAsyncClient {
  return new TemporalAsyncClient({ ...(activityConfig ? { activityConfig } : {}), runner });
}
