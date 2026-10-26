// The Gemini client a workflow holds (Python's _temporal_async_client.py): each module it exposes
// runs its network calls in activities on the worker's real client.

import type { ActivityOptions } from "@temporalio/workflow";
import type { AgentWorkflowRunner } from "../../harness/agent-workflow.ts";
import { TemporalAsyncInteractions } from "./temporal-interactions.ts";

export class TemporalAsyncClient {
  readonly interactions: TemporalAsyncInteractions;

  constructor({
    activityConfig,
    runner = null,
  }: { activityConfig?: ActivityOptions; runner?: AgentWorkflowRunner | null } = {}) {
    this.interactions = new TemporalAsyncInteractions(runner, activityConfig);
  }
}
