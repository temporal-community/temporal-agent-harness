// The Interactions activity's retry posture on the errors the real SDK raises (Python's
// test_client_errors.py): a 4xx other than 408/429 fails it non-retryably with
// {status_code, message}, while 408, 429 and 5xx stay retryable and the SDK itself never retries.
// The SDK talks to a local HTTP server, so its errors are the ones a real reply produces.

import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, describe, it } from "node:test";
import { GoogleGenAI } from "@google/genai";
import { ApplicationFailure } from "@temporalio/common";
import {
  makeGeminiInteractionsCreateStreamed,
  translateGeminiError,
  type GeminiInteractionsClient,
} from "../../../src/ai-sdks/google-genai-plugin/index.ts";
import { completed, created, text, type Event } from "./script.ts";

const TURN_ORDER_MESSAGE =
  "Please ensure that function response turn comes immediately after a function call turn.";
const NON_RETRYABLE_STATUSES = [400, 401, 403, 404, 409, 422];
const RETRYABLE_STATUSES = [408, 429, 500, 503];
const PARAMS = { model: "gemini-test", input: "Hi", stream: true } as const;

let server: Server;
let requests = 0;
let reply: { status: number; body: string; contentType: string } = {
  status: 200,
  body: "",
  contentType: "application/json",
};
let client: GeminiInteractionsClient;

before(async () => {
  server = createServer((request, response) => {
    requests += 1;
    request.resume();
    request.on("end", () => {
      response.writeHead(reply.status, { "content-type": reply.contentType });
      response.end(reply.body);
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  client = new GoogleGenAI({
    apiKey: "test-key",
    httpOptions: { baseUrl: `http://127.0.0.1:${port}` },
  });
});
after(async () => {
  await new Promise((resolve) => server.close(resolve));
});

/** The next request answers HTTP `status` with the API's error body. */
function failWith(status: number) {
  requests = 0;
  reply = {
    status,
    contentType: "application/json",
    body: JSON.stringify({
      error: { code: status, message: TURN_ORDER_MESSAGE, status: "INVALID_ARGUMENT" },
    }),
  };
}

async function sdkError(status: number): Promise<Error> {
  failWith(status);
  try {
    for await (const _ of await client.interactions.create(PARAMS, {
      retries: { strategy: "none" },
    }));
  } catch (e) {
    assert.ok(e instanceof Error);
    return e;
  }
  throw new Error("the SDK did not fail");
}

const activity = () => makeGeminiInteractionsCreateStreamed(client);

describe("the Gemini error translation", () => {
  for (const status of NON_RETRYABLE_STATUSES)
    it(`fails a ${status} non-retryably with its status code and the API's message`, async () => {
      failWith(status);
      await assert.rejects(activity()(PARAMS, null), (e) => {
        assert.ok(e instanceof ApplicationFailure);
        assert.equal(e.nonRetryable, true);
        assert.match(e.type ?? "", /Error$/);
        assert.equal(e.message, `Gemini API returned ${status}: ${TURN_ORDER_MESSAGE}`);
        assert.deepStrictEqual(e.details, [{ status_code: status, message: TURN_ORDER_MESSAGE }]);
        assert.ok(e.cause instanceof Error);
        assert.equal((e.cause as Error & { status?: number }).status, status);
        return true;
      });
      assert.equal(requests, 1);
    });

  for (const status of RETRYABLE_STATUSES)
    it(`leaves a ${status} retryable and the SDK does not retry it`, async () => {
      failWith(status);
      await assert.rejects(activity()(PARAMS, null), (e) => {
        assert.ok(!(e instanceof ApplicationFailure));
        assert.equal((e as { status?: number }).status, status);
        return true;
      });
      assert.equal(requests, 1);
    });

  it("leaves a network error retryable", () => {
    const refused = Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" });
    assert.equal(translateGeminiError(refused), refused);
  });

  it("fails a client error raised mid-stream non-retryably", async () => {
    const original = await sdkError(400);
    const midStream: GeminiInteractionsClient = {
      interactions: {
        create: async () =>
          (async function* (): AsyncGenerator<Event> {
            yield created();
            throw original;
          })(),
      },
    };
    await assert.rejects(makeGeminiInteractionsCreateStreamed(midStream)(PARAMS, null), (e) => {
      assert.ok(e instanceof ApplicationFailure);
      assert.equal(e.nonRetryable, true);
      assert.equal(e.cause, original);
      return true;
    });
  });

  it("returns the events of a real SDK stream", async () => {
    const events = [created(), ...text(0, "Hel", "lo"), completed()];
    requests = 0;
    reply = {
      status: 200,
      contentType: "text/event-stream",
      body: events.map((e) => `event: ${e.event_type}\ndata: ${JSON.stringify(e)}\n\n`).join(""),
    };
    const result = await activity()(PARAMS, null);
    assert.deepStrictEqual(
      result.events.map((e) => e.event_type),
      events.map((e) => e.event_type),
    );
    const deltas = result.events.flatMap((e) =>
      e.event_type === "step.delta" && e.delta.type === "text" ? [e.delta.text] : [],
    );
    assert.deepStrictEqual(deltas, ["Hel", "lo"]);
  });
});
