import { describe, expect, it } from "vitest";
import { buildReplayLog } from "./replayLog";

describe("replay-log presentation adapter", () => {
  it("uses an injected message adapter for a structured turn-start row", () => {
    const log = buildReplayLog([
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

    expect(log.rows).toMatchObject([
      { event: "turn_started", body: "Extension request" }
    ]);
  });

  it("uses an injected reply adapter for an opaque reply row", () => {
    const log = buildReplayLog([
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

    expect(log.rows).toMatchObject([
      { event: "reply", body: "Extension reply" }
    ]);
  });

  it("uses an injected message adapter for a structured queued-message row", () => {
    const log = buildReplayLog([
      {
        event: "message_queued",
        data: {
          type: "message_queued",
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

    expect(log.rows).toMatchObject([
      { event: "message_queued", body: "Extension request" }
    ]);
  });
});
