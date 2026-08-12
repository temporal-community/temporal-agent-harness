import { afterEach, describe, expect, it, vi } from "vitest";
import { HttpAgentApi } from "./httpClient";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("HttpAgentApi", () => {
  it("preserves the response status on a definitive message rejection", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ message: "The reviewed draft is stale." }),
      { status: 422, headers: { "Content-Type": "application/json" } }
    )));
    const api = new HttpAgentApi();

    await expect(api.submitMessage({
      session_id: "session-1",
      message: "Hello",
      expected_turn: 1
    })).rejects.toMatchObject({
      status: 422,
      message: "The reviewed draft is stale."
    });
  });
});
