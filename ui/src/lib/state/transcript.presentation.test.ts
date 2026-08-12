import { describe, expect, it } from "vitest";
import { buildTranscript } from "./transcript";

describe("transcript presentation adapter", () => {
  it("uses an injected message adapter for a structured user message", () => {
    const items = buildTranscript([
      {
        event: "turn_started",
        data: {
          type: "turn_started",
          agent_id: "agent-1",
          turn_id: "turn-1",
          turn_number: 1,
          timestamp: 1,
          user_message: JSON.stringify({
            type: "example_action",
            payload: { opaque: true }
          })
        }
      }
    ] as never, {
      messageText: () => "Extension request"
    });

    expect(items).toMatchObject([
      { kind: "user", text: "Extension request" }
    ]);
  });

  it("uses an injected reply adapter for an opaque structured reply", () => {
    const items = buildTranscript([
      {
        event: "reply",
        data: {
          type: "reply",
          agent_id: "agent-1",
          turn_id: "turn-1",
          turn_number: 1,
          timestamp: 1,
          output: { opaque: true }
        }
      }
    ] as never, {
      messageText: () => "unused",
      replyText: () => "Extension reply"
    });

    expect(items).toMatchObject([
      { kind: "agent", text: "Extension reply" }
    ]);
  });
});
