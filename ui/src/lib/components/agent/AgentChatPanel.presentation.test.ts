import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import AgentChatPanel from "./AgentChatPanel.svelte";
import type { AgentPresentationAdapter } from "$lib/state/messagePresentation";

describe("AgentChatPanel presentation adapter", () => {
  it("formats a structured session initial message with the injected adapter", () => {
    const structuredMessage = JSON.stringify({
      type: "structured_request",
      payload: { document: "meeting notes" }
    });
    const presentation: AgentPresentationAdapter = {
      messageText: (value) =>
        value === structuredMessage ? "Prepare the meeting notes" : "Unexpected request"
    };

    const { body } = render(AgentChatPanel, {
      props: {
        items: [],
        sessions: [{
          workflow_id: "session-42",
          created_at: 1,
          label: "Session 42",
          agent_workflow_type: "GeneralAgent",
          is_message_queuing_enabled: false,
          initial_user_message: structuredMessage
        }],
        agentLabel: "General agent",
        sessionId: "session-42",
        presentation
      }
    });

    expect(body).toContain("Prepare the meeting notes");
    expect(body).not.toContain("structured_request");
  });
});
