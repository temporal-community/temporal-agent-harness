// HttpTransport: the requests it makes of the harness web API, and how it reads the answers.

import assert from "node:assert/strict";
import { test } from "node:test";

import type { AgentSseFrame } from "../src/frames.ts";
import { HttpError, HttpTransport, readSse } from "../src/transport.ts";

interface Call {
  url: string;
  method: string;
  headers: Headers;
  body: unknown;
  signal: AbortSignal | null | undefined;
}

/** A `fetch` that records every request and answers each with the next response in line. */
function stubFetch(...responses: Response[]) {
  const calls: Call[] = [];
  const fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    calls.push({
      url: String(input),
      method: init.method ?? "GET",
      headers: new Headers(init.headers),
      body: typeof init.body === "string" ? JSON.parse(init.body) : init.body,
      signal: init.signal
    });
    const response = responses.shift();
    assert.ok(response, `unexpected request to ${String(input)}`);
    return response;
  };
  return { calls, fetch: fetch as typeof globalThis.fetch };
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** An event-stream body delivered in exactly these chunks, so a test controls where they split. */
function sse(...chunks: Array<string | Uint8Array>): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
      }
      controller.close();
    }
  });
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

async function collect(frames: AsyncIterable<AgentSseFrame>): Promise<AgentSseFrame[]> {
  const out: AgentSseFrame[] = [];
  for await (const frame of frames) out.push(frame);
  return out;
}

/** One frame exactly as `temporal_agent_harness.web.app._sse` writes it. */
const frame = (event: string, data: Record<string, unknown>) =>
  `event: ${event}\ndata: ${JSON.stringify({ type: event, ...data })}\n\n`;

test("attach asks for the session from an offset and yields each frame", async () => {
  const { calls, fetch } = stubFetch(
    sse(frame("turn_started", { resume_offset: 8 }), frame("turn_end", { resume_offset: 9 }))
  );
  const transport = new HttpTransport({ baseUrl: "http://h/api", fetch });
  const signal = new AbortController().signal;
  const frames = await collect(transport.attach("s/1 ?", 7, signal));

  assert.equal(calls[0]!.url, "http://h/api/attach?session_id=s%2F1%20%3F&from_offset=7");
  assert.equal(calls[0]!.signal, signal);
  assert.deepEqual(
    frames.map((f) => [f.event, f.data.resume_offset]),
    [["turn_started", 8], ["turn_end", 9]]
  );
});

test("a frame split across chunks, or mid-character, comes out whole", async () => {
  const text = frame("reply_delta", { text: "héllo" });
  const bytes = new TextEncoder().encode(text);
  const cut = bytes.indexOf(0xc3) + 1; // between the two bytes of "é"
  const frames = await collect(
    readSse(sse(bytes.slice(0, 5), bytes.slice(5, cut), bytes.slice(cut), frame("turn_end", {})))
  );
  assert.deepEqual(
    frames.map((f) => f.event),
    ["reply_delta", "turn_end"]
  );
  assert.equal((frames[0]!.data as { text?: string }).text, "héllo");
});

test("blocks without both an event and data, like keep-alive comments, are skipped", async () => {
  const frames = await collect(
    readSse(sse(": ping\n\n", "event: orphan\n\n", frame("turn_end", {}), "event: trailing"))
  );
  assert.deepEqual(
    frames.map((f) => f.event),
    ["turn_end"]
  );
});

test("a response with no body yields nothing", async () => {
  assert.deepEqual(await collect(readSse(new Response(null))), []);
});

test("messages are POSTed as JSON with the transport's own headers merged in", async () => {
  const reply = { turn_number: 1, turn_id: "t", message_id: "m", accepted_offset: 3, disposition: "opened" };
  const { calls, fetch } = stubFetch(json(reply));
  const transport = new HttpTransport({ baseUrl: "http://h/api/", fetch, headers: { Authorization: "Bearer x" } });

  assert.deepEqual(await transport.submitMessage("s1", { type: "play", payload: { cell: 5 } }), reply);
  const call = calls[0]!;
  assert.equal(call.url, "http://h/api/messages");
  assert.equal(call.method, "POST");
  assert.equal(call.headers.get("content-type"), "application/json");
  assert.equal(call.headers.get("authorization"), "Bearer x");
  assert.deepEqual(call.body, { session_id: "s1", message: { type: "play", payload: { cell: 5 } } });
});

test("an approval and a callback result are flattened into their request bodies", async () => {
  const { calls, fetch } = stubFetch(json({}), json({}), json({}));
  const transport = new HttpTransport({ baseUrl: "http://h/api/", fetch });

  await transport.approveTool("s1", "tool-1", { approved: true, remember: true });
  await transport.provideCallbackResult("s1", "tool-2", { result: { ok: 1 } });
  await transport.provideCallbackResult("s1", "tool-3", { error: "boom" });

  assert.deepEqual(
    calls.map((c) => [c.method, c.url, c.body]),
    [
      ["POST", "http://h/api/approve", { session_id: "s1", tool_id: "tool-1", approved: true, remember: true }],
      ["POST", "http://h/api/callback-result", { session_id: "s1", tool_id: "tool-2", result: { ok: 1 } }],
      ["POST", "http://h/api/callback-result", { session_id: "s1", tool_id: "tool-3", error: "boom" }]
    ]
  );
});

test("status, workflow status and close address the session in the path", async () => {
  const status = { current_turn: 2, turn_active: false, pending_turns: [] };
  const workflow = { workflow_id: "a/b", execution_status: "RUNNING", closed: false };
  const { calls, fetch } = stubFetch(json(status), json(workflow), json({}));
  const transport = new HttpTransport({ baseUrl: "http://h/api/", fetch });

  assert.deepEqual(await transport.agentStatus("a/b"), status);
  assert.deepEqual(await transport.workflowStatus("a/b"), workflow);
  await transport.closeSession("a/b");

  assert.deepEqual(
    calls.map((c) => [c.method, c.url]),
    [
      ["GET", "http://h/api/status/a%2Fb"],
      ["GET", "http://h/api/workflow-status/a%2Fb"],
      ["POST", "http://h/api/sessions/a%2Fb/close"]
    ]
  );
});

test("the default base is `api/`, relative to the page", async () => {
  const { calls, fetch } = stubFetch(json({}));
  await new HttpTransport({ fetch }).closeSession("s1");
  assert.equal(calls[0]!.url, "api/sessions/s1/close");
});

test("a harness error body becomes an HttpError carrying its code and message", async () => {
  const { fetch } = stubFetch(json({ error: "mid_turn_rejected", message: "play cannot run mid-turn" }, 409));
  const transport = new HttpTransport({ fetch });
  await assert.rejects(
    transport.submitMessage("s1", { type: "play", payload: {} }),
    (error: unknown) => {
      assert.ok(error instanceof HttpError);
      assert.equal(error.status, 409);
      assert.equal(error.code, "mid_turn_rejected");
      assert.equal(error.message, "play cannot run mid-turn");
      return true;
    }
  );
});

test("an error body without a harness message falls back to the status, or the raw text", async () => {
  const { fetch } = stubFetch(
    json({ detail: "Not Found" }, 404),
    new Response("upstream timed out", { status: 502, statusText: "Bad Gateway" }),
    new Response("", { status: 500, statusText: "Internal Server Error" })
  );
  const transport = new HttpTransport({ fetch });
  const failure = async (call: Promise<unknown>) => {
    try {
      await call;
    } catch (error) {
      assert.ok(error instanceof HttpError);
      return [error.status, error.code, error.message];
    }
    assert.fail("the request did not fail");
  };

  /* FastAPI's own 404 body is `{"detail": ...}`, which is not a harness error. */
  assert.deepEqual(await failure(transport.agentStatus("s1")), [404, null, "404"]);
  assert.deepEqual(await failure(transport.agentStatus("s1")), [502, null, "upstream timed out"]);
  assert.deepEqual(await failure(transport.agentStatus("s1")), [500, null, "500 Internal Server Error"]);
});

test("an attach that fails throws before yielding anything", async () => {
  const { fetch } = stubFetch(json({ error: "not_found", message: "no such session" }, 404));
  const transport = new HttpTransport({ fetch });
  await assert.rejects(
    collect(transport.attach("s1", 0, new AbortController().signal)),
    /no such session/
  );
});
