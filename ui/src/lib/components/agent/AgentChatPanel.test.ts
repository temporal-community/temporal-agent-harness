import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import AgentChatPanel, { submitComposerMessage } from "./AgentChatPanel.svelte";

describe("AgentChatPanel", () => {
  it("restores a composer draft when its submit is rejected", async () => {
    const outbound = "Keep this message";

    await expect(submitComposerMessage(
      async () => { throw new Error("HTTP 422: rejected"); },
      outbound,
      outbound
    )).resolves.toEqual({ sent: false, draft: outbound });
  });

  it("omits remembered approval when the backend forbids it but keeps approve and reject", () => {
    const { body } = render(AgentChatPanel, {
      props: {
        items: [],
        logs: [{
          id: "approval-1",
          index: 1,
          ordinal: 1,
          turnNumber: 1,
          sourceTurnNumber: 1,
          turnId: "turn-1",
          timestamp: 1,
          event: "tool_approval_requested",
          actor: "approval",
          tone: "approval",
          label: "Approval requested",
          body: "lookup_records",
          toolId: "tool-1",
          toolName: "lookup_records",
          input: { record_id: "record-1" },
          status: "awaiting",
          citations: [],
          rememberAllowed: false
        }],
        agentLabel: "Generic agent",
        sessionId: "agent-session-1"
      }
    });

    expect(body).not.toContain("Approve and remember");
    expect(body).toMatch(/class="approval-approve/);
    expect(body).toContain("Reject");
  });

  it("keeps approvals with the same tool ID pending for their own workflows", () => {
    const log = (workflowId: string, event: "tool_approval_requested" | "tool_approval_resolved") => ({
      id: `${workflowId}-${event}`,
      index: 1,
      ordinal: 1,
      turnNumber: 1,
      sourceTurnNumber: 1,
      workflowId,
      turnId: "turn-1",
      timestamp: 1,
      event,
      actor: "approval" as const,
      tone: "approval" as const,
      label: event === "tool_approval_requested" ? "Approval requested" : "Approval resolved",
      body: "lookup_records",
      toolId: "shared-tool",
      toolName: "lookup_records",
      input: {},
      status: event === "tool_approval_requested" ? "awaiting" : "approved",
      citations: []
    });
    const { body } = render(AgentChatPanel, {
      props: {
        items: [],
        logs: [
          log("root-workflow", "tool_approval_requested"),
          log("child-workflow", "tool_approval_resolved")
        ],
        agentLabel: "Generic agent",
        sessionId: "root-workflow"
      }
    });

    expect(body).toContain("1 approval needed");
    expect(body).toContain("lookup_records");
  });
});
