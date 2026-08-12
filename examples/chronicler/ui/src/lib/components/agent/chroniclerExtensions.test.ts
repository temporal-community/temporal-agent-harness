import { afterEach, describe, expect, it } from "vitest";
import { render } from "svelte/server";
import AgentChatPanel from "$lib/components/agent/AgentChatPanel.svelte";
import {
  chroniclerToolPresentation,
  clearChroniclerAudioPresentation,
  setChroniclerAudioPresentation
} from "./chroniclerExtensions.svelte";

afterEach(() => clearChroniclerAudioPresentation());

describe("Chronicler inline tool extension", () => {
  it("nests one generation card in the selected generate_audio activity and expands its feed", () => {
    setChroniclerAudioPresentation({
      snapshot: {
        child_workflow_id: "chronicler-audio--agent-session-1",
        state: "running",
        status: {
          generation_id: "generation-1",
          child_workflow_id: "chronicler-audio--agent-session-1",
          phase: "generating_audio",
          detail: "Generating approved audio."
        },
        result: null,
        receipts: [],
        pending_destination_revision: null
      },
      generationId: "generation-1",
      cancellation: { enabled: false, detail: "Standby" },
      destinationApproval: null,
      destinationAuthority: { ready: false, detail: "Unavailable" }
    });

    const { body } = render(AgentChatPanel, {
      props: {
        items: [{ kind: "user", id: "user-1", turnNumber: 1, text: "Generate it", timestamp: 1 }],
        logs: [{
          id: "tool-1",
          index: 1,
          ordinal: 1,
          turnNumber: 1,
          sourceTurnNumber: 1,
          turnId: "turn-1",
          timestamp: 1,
          event: "tool_start",
          actor: "tool",
          tone: "tool",
          label: "Tool started",
          body: "generate_audio",
          toolId: "tool-1",
          toolName: "generate_audio",
          input: { generation_id: "generation-1" },
          status: "running",
          citations: []
        }],
        agentLabel: "Chronicler",
        sessionId: "agent-session-1",
        following: false,
        closed: false,
        toolPresentation: chroniclerToolPresentation
      }
    });

    expect(body.match(/class="generation-card/g)).toHaveLength(1);
    expect(body).toMatch(/data-activity-id="tool-1"[^]*generation-card/);
    expect(body).toContain('class="activity-feed expanded');
  });
});
