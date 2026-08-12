import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import AgentChatPanel from "./AgentChatPanel.svelte";
import ToolActivityAttachment from "../../../test/fixtures/ToolActivityAttachment.svelte";
import type { ReplayLogRow } from "$lib/state/replayLog";

describe("AgentChatPanel tool presentation", () => {
  it("renders an attachment for the selected tool activity with neutral chat props", () => {
    const onSend = vi.fn();
    const row: ReplayLogRow = {
      id: "tool-1",
      index: 1,
      ordinal: 1,
      turnNumber: 1,
      sourceTurnNumber: 1,
      turnId: "turn-1",
      timestamp: 1,
      event: "tool_end" as const,
      actor: "tool" as const,
      tone: "done" as const,
      label: "Tool completed",
      body: "lookup",
      toolId: "tool-1",
      toolName: "lookup",
      status: "done",
      citations: []
    };

    const { body } = render(AgentChatPanel, {
      props: {
        items: [{ kind: "user", id: "message-1", turnNumber: 1, text: "Find it", timestamp: 1 }],
        logs: [row],
        agentLabel: "General agent",
        sessionId: "session-42",
        following: false,
        closed: false,
        onSend,
        toolPresentation: {
          attachment: ToolActivityAttachment,
          isHost: (candidate, rows) =>
            candidate.id === row.id && rows.length === 1
        }
      }
    });

    expect(body).toContain("data-tool-activity-attachment");
    expect(body).toContain(
      "tool=lookup;session=session-42;following=false;closed=false;send=function"
    );
  });
});
