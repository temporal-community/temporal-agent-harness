// ABOUTME: Pins that a failed request keeps the server's `error` code, not just its message. The
// approval card needs it to tell "someone already answered this" — another tab, or an "Always
// allow" rule — from a real failure: the first is a muted "Already decided", the second is red.
// The code string is what temporal_agent_harness/web/app.py forwards from the workflow's update
// validator, so a rename on either side is caught here.
//
// Also pins how a failed body becomes the message a card shows. FastAPI answers a 422 with
// `{detail: [{loc, msg}]}` and an HTTPException with `{detail: "..."}`, and a proxy in front
// answers with an HTML page; the card used to show the raw JSON or markup at any length.

import assert from "node:assert/strict";
import { afterEach, describe, it } from "vitest";

import { ApiError, HttpAgentApi, approvalAlreadyResolved, errorFromBody } from "./httpClient.ts";

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

const FALLBACK = "Request failed (502)";

describe("errorFromBody", () => {
  it("reads the harness's message and keeps its error code", () => {
    const error = errorFromBody(
      JSON.stringify({ error: "ToolApprovalAlreadyResolved", message: "Already decided." }),
      FALLBACK
    );
    assert.ok(error instanceof ApiError);
    assert.equal(error.message, "Already decided.");
    assert.equal(error.code, "ToolApprovalAlreadyResolved");
  });

  it("reads FastAPI's detail, as a string or a list of validation errors", () => {
    assert.equal(errorFromBody(JSON.stringify({ detail: "Not found" }), FALLBACK).message, "Not found");
    const listed = errorFromBody(
      JSON.stringify({
        detail: [
          { loc: ["body", "result"], msg: "field required", type: "missing" },
          { loc: ["body", "tool_id"], msg: "str type expected", type: "type_error" }
        ]
      }),
      FALLBACK
    );
    assert.equal(listed.message, "field required; str type expected");
    assert.equal(listed.code, null);
  });

  it("never shows an HTML error page, and falls back for an unreadable body", () => {
    const html = "<!DOCTYPE html><html><body><h1>502 Bad Gateway</h1><hr>nginx</body></html>";
    assert.equal(errorFromBody(html, FALLBACK).message, FALLBACK);
    assert.equal(errorFromBody("", FALLBACK).message, FALLBACK);
    assert.equal(errorFromBody(JSON.stringify({ detail: [] }), FALLBACK).message, FALLBACK);
    assert.equal(errorFromBody("upstream timed out", FALLBACK).message, "upstream timed out");
  });

  it("cuts a long message to a length a card can hold", () => {
    const message = errorFromBody(JSON.stringify({ message: "x".repeat(5_000) }), FALLBACK).message;
    assert.equal(message.length, 501);
    assert.ok(message.endsWith("…"));
  });
});
