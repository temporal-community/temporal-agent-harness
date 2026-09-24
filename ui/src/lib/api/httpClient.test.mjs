// ABOUTME: Pins that a failed request keeps the server's `error` code, not just its message. The
// approval card needs it to tell "someone already answered this" — another tab, or an "Always
// allow" rule — from a real failure: the first is a muted "Already decided", the second is red.
// The code string is what temporal_agent_harness/web/app.py forwards from the workflow's update
// validator, so a rename on either side is caught here.

import assert from "node:assert/strict";
import { afterEach, describe, it } from "vitest";

import { ApiError, HttpAgentApi, approvalAlreadyResolved } from "./httpClient.ts";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function respondWith(status, body) {
  globalThis.fetch = async () =>
    new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
}

async function approvalFailure() {
  try {
    await new HttpAgentApi().approve({ session_id: "wf", tool_id: "t1", approved: true });
  } catch (error) {
    return error;
  }
  assert.fail("the approval should have been rejected");
}

describe("a failed approval", () => {
  it("keeps the server's code alongside its message", async () => {
    respondWith(409, { error: "ToolApprovalAlreadyResolved", message: "tool t1 already resolved" });
    const error = await approvalFailure();

    assert.ok(error instanceof ApiError);
    assert.equal(error.code, "ToolApprovalAlreadyResolved");
    assert.equal(error.message, "tool t1 already resolved");
    assert.ok(approvalAlreadyResolved(error), "an approval answered elsewhere is not a failure");
  });

  it("still reads every other rejection as a failure", async () => {
    respondWith(409, { error: "UnknownToolApproval", message: "no pending approval t1" });
    assert.equal(approvalAlreadyResolved(await approvalFailure()), false);

    respondWith(500, "upstream exploded");
    const error = await approvalFailure();
    assert.equal(error.code, null);
    assert.equal(error.message, "upstream exploded");
    assert.equal(approvalAlreadyResolved(error), false);
    assert.equal(approvalAlreadyResolved(new Error("ToolApprovalAlreadyResolved")), false);
  });
});
