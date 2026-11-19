// startSession and HttpTransport's session calls: what they send to `POST /api/sessions` and how
// a rejected start surfaces.

import assert from "node:assert/strict";
import { test } from "node:test";

import { startSession, type SessionSummary } from "../src/start.ts";
import { HttpError, HttpTransport } from "../src/transport.ts";

interface Call {
  url: string;
  method: string;
  body: unknown;
}

function stubFetch(...responses: Response[]) {
  const calls: Call[] = [];
  const fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    calls.push({
      url: String(input),
      method: init.method ?? "GET",
      body: typeof init.body === "string" ? JSON.parse(init.body) : init.body
    });
    const response = responses.shift();
    assert.ok(response, `unexpected request to ${String(input)}`);
    return response;
  };
  return { calls, fetch: fetch as typeof globalThis.fetch };
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const created: SessionSummary = {
  workflow_id: "agent-session-1",
  created_at: 0,
  label: "Trip",
  agent_workflow_type: "TripAgent",
  execution_status: "RUNNING",
  closed: false
};

test("startSession posts the workflow type and the init data", async () => {
  const { calls, fetch } = stubFetch(json(created));
  const transport = new HttpTransport({ baseUrl: "http://h/api", fetch });

  const session = await startSession(
    transport,
    { workflowType: "TripAgent" },
    { data: { traveler: "Ada" }, sessionId: "trip-1" }
  );

  assert.equal(session.workflow_id, "agent-session-1");
  assert.deepEqual(calls, [
    {
      url: "http://h/api/sessions",
      method: "POST",
      body: { agent_workflow_type: "TripAgent", session_id: "trip-1", data: { traveler: "Ada" } }
    }
  ]);
});

test("startSession without options sends only the workflow type", async () => {
  const { calls, fetch } = stubFetch(json(created));
  await startSession(new HttpTransport({ fetch }), { workflowType: "TripAgent" });
  assert.deepEqual(calls[0]!.body, { agent_workflow_type: "TripAgent" });
  assert.equal(calls[0]!.url, "api/sessions");
});

test("a start the server rejects is an HttpError carrying its code", async () => {
  const { fetch } = stubFetch(
    json(
      { error: "invalid_init_data", message: "TripAgent requires init data (Trip).", errors: [] },
      422
    )
  );
  await assert.rejects(
    startSession(new HttpTransport({ fetch }), { workflowType: "TripAgent" }),
    (error: unknown) =>
      error instanceof HttpError &&
      error.status === 422 &&
      error.code === "invalid_init_data" &&
      error.message === "TripAgent requires init data (Trip)."
  );
});

test("listSessions reads a page of GET sessions", async () => {
  const first = { sessions: [created], next_page_token: "t1" };
  const last = { sessions: [], next_page_token: null };
  const { calls, fetch } = stubFetch(json(first), json(last));
  const transport = new HttpTransport({ fetch });
  assert.deepEqual(await transport.listSessions(), first);
  assert.deepEqual(await transport.listSessions({ pageSize: 50, pageToken: "t1" }), last);
  assert.deepEqual(calls, [
    { url: "api/sessions", method: "GET", body: undefined },
    { url: "api/sessions?page_size=50&page_token=t1", method: "GET", body: undefined }
  ]);
});

test("getSession reads GET sessions/{id}", async () => {
  const { calls, fetch } = stubFetch(json(created));
  assert.deepEqual(await new HttpTransport({ fetch }).getSession("slack:C1/1"), created);
  assert.deepEqual(calls, [{ url: "api/sessions/slack%3AC1%2F1", method: "GET", body: undefined }]);
});
