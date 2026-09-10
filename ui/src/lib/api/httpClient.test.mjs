import assert from "node:assert/strict";
import { afterEach, describe, it, vi } from "vitest";

import { HttpAgentApi } from "./httpClient.ts";

afterEach(() => vi.unstubAllGlobals());

describe("attach connection notification", () => {
  it("reports successful headers even while the body has no new actions", async () => {
    let streamController;
    const body = new ReadableStream({ start(controller) { streamController = controller; } });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, {
      headers: { "content-type": "text/event-stream" }
    })));
    const connected = vi.fn();
    const stream = new HttpAgentApi().attach("codex-demo", 8, undefined, connected);
    const pending = stream.next();
    await vi.waitFor(() => assert.equal(connected.mock.calls.length, 1));
    streamController.close();
    assert.deepEqual(await pending, { value: undefined, done: true });
  });

  it("does not announce a rejected attach as connected", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Unavailable", { status: 503 })));
    const connected = vi.fn();
    const stream = new HttpAgentApi().attach("codex-demo", 8, undefined, connected);
    await assert.rejects(stream.next(), /Unavailable/);
    assert.equal(connected.mock.calls.length, 0);
  });
});
