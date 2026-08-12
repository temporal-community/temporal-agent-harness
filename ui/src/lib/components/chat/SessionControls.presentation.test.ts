import { describe, expect, it } from "vitest";
import { sessionInitialMessageText } from "./SessionControls.svelte";
import type { AgentPresentationAdapter } from "$lib/state/messagePresentation";

describe("SessionControls presentation adapter", () => {
  it("formats a structured initial message for the session picker and delete label", () => {
    const structuredMessage = JSON.stringify({
      type: "structured_request",
      payload: { document: "meeting notes" }
    });
    const presentation: AgentPresentationAdapter = {
      messageText: (value) =>
        value === structuredMessage ? "Prepare the meeting notes" : "Unexpected request"
    };

    const text = sessionInitialMessageText({
      workflow_id: "session-42",
      created_at: 1,
      label: "Session 42",
      agent_workflow_type: "GeneralAgent",
      is_message_queuing_enabled: false,
      initial_user_message: structuredMessage
    }, presentation);

    expect(text).toBe("Prepare the meeting notes");
    expect(text).not.toContain("structured_request");
  });
});
