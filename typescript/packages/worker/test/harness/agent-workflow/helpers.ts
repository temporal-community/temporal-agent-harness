// Helpers shared by the agent workflow tests: status, the agent client and update failures.

import assert from "node:assert/strict";
import type { ApplicationFailure } from "@temporalio/common";
import { WorkflowUpdateFailedError, type WorkflowHandle } from "@temporalio/client";
import {
  AGENT_STATUS_QUERY,
  type AgentStatus,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { AgentClient } from "../../../src/harness/agent-client.ts";
import type { TestEnv } from "../../support.ts";

export const status = (h: WorkflowHandle) => h.query<AgentStatus>(AGENT_STATUS_QUERY);

export const agentClient = (env: TestEnv, h: WorkflowHandle) =>
  new AgentClient(env.client, h.workflowId);

/** The failure an update was rejected with; fails if it was not rejected. */
export async function updateFailure(promise: Promise<unknown>): Promise<ApplicationFailure> {
  try {
    await promise;
  } catch (e) {
    assert.ok(e instanceof WorkflowUpdateFailedError, `expected an update failure, got ${e}`);
    return e.cause as ApplicationFailure;
  }
  assert.fail("expected the update to fail");
}

/** The rejection `promise` settles with; fails if it resolves. */
export async function rejection(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (e) {
    return e;
  }
  assert.fail("expected a rejection");
}
