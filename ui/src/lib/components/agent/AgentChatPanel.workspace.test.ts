import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import AgentChatPanel from "./AgentChatPanel.svelte";
import WorkspaceExtension from "../../../test/fixtures/WorkspaceExtension.svelte";

describe("AgentChatPanel workspace extension", () => {
  it("does not render a workspace extension when none is supplied", () => {
    const { body } = render(AgentChatPanel, {
      props: {
        items: [],
        agentLabel: "General agent",
        sessionId: "session-42"
      }
    });

    expect(body).not.toContain("data-workspace-extension");
  });

  it("renders a supplied workspace extension with neutral chat props", () => {
    const onSend = vi.fn();

    const { body } = render(AgentChatPanel, {
      props: {
        items: [{ kind: "user", id: "message-1", turnNumber: 1, text: "Hello", timestamp: 1 }],
        agentLabel: "General agent",
        sessionId: "session-42",
        following: false,
        closed: true,
        onSend,
        workspaceComponent: WorkspaceExtension
      }
    });

    expect(body).toContain(
      "items=1;session=session-42;following=false;closed=true;send=function"
    );
  });
});
