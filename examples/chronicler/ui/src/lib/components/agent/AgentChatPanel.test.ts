import { afterEach, describe, expect, it } from "vitest";
import { render } from "svelte/server";
import AgentChatPanel from "$lib/components/agent/AgentChatPanel.svelte";
import ChroniclerAudioWorkspaceExtension from "./ChroniclerAudioWorkspaceExtension.svelte";
import {
  chroniclerToolPresentation,
  clearChroniclerAudioPresentation,
  setChroniclerAudioPresentation
} from "./chroniclerExtensions.svelte";

afterEach(() => clearChroniclerAudioPresentation());

function ancestorClassesAt(html: string, marker: string): string[] {
  const markerIndex = html.indexOf(marker);
  const stack: Array<{ tag: string; classes: string[] }> = [];
  const voidTags = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]);

  for (const match of html.slice(0, markerIndex).matchAll(/<(\/)?([a-z][\w-]*)([^>]*)>/gi)) {
    const [, closing, rawTag, attributes] = match;
    const tag = rawTag.toLowerCase();
    if (closing) {
      const openIndex = stack.findLastIndex((entry) => entry.tag === tag);
      if (openIndex >= 0) stack.splice(openIndex);
      continue;
    }
    if (voidTags.has(tag) || attributes.trimEnd().endsWith("/")) continue;
    const classes = attributes.match(/\bclass="([^"]*)"/)?.[1]?.split(/\s+/).filter(Boolean) ?? [];
    stack.push({ tag, classes });
  }

  return stack.flatMap((entry) => entry.classes);
}

describe("AgentChatPanel Chronicler audio integration", () => {
  it("mounts the specialized audio workspace only when explicitly provided", () => {
    const base = {
      items: [],
      agentLabel: "Chronicler",
      sessionId: "agent-session-1"
    };
    const chronicler = render(AgentChatPanel, {
      props: { ...base, workspaceComponent: ChroniclerAudioWorkspaceExtension }
    }).body;
    const other = render(AgentChatPanel, { props: base }).body;

    expect(chronicler).toContain("Create spoken recap");
    expect(other).not.toContain("Create spoken recap");
  });

  it("renders transcript-derived audio review as read-only during historical replay", () => {
    const { body } = render(AgentChatPanel, {
      props: {
        items: [{
          kind: "agent",
          output: {
            draft: {
              draft_id: "draft-1",
              draft_digest: "draft-digest",
              source_kind: "synthetic",
              source_identity: "topic-1",
              source_content: "# Synthetic Transcript\nThe bell tolls.",
              source_hash: "source-hash",
              recap_script: "The bell tolls.",
              voice: "Charon",
              wav_path: "audio/recap.wav",
              synthetic_markdown_path: "audio/recap.md",
              bridge_id: "bridge-1",
              root_id: "root-1",
              folder_binding_id: "binding-1"
            }
          }
        }] as never,
        agentLabel: "Chronicler",
        sessionId: "agent-session-1",
        workspaceComponent: ChroniclerAudioWorkspaceExtension,
        following: false
      }
    });

    expect(body).toContain("Historical replay");
    expect(body).toContain("The bell tolls.");
    expect(body).toMatch(/Approve and generate<\/button>/);
    expect(body).toMatch(/<button[^>]*class="approve[^"]*"[^>]*disabled/);
    expect(ancestorClassesAt(body, "Historical replay")).toContain("message-list");
    expect(body.indexOf("Historical replay")).toBeLessThan(body.indexOf('class="composer-wrap'));
  });

  it("nests the generation card inside the actual generate_audio activity row", () => {
    const snapshot = {
      child_workflow_id: "chronicler-audio--agent-session-1",
      state: "running" as const,
      status: {
        generation_id: "generation-1",
        child_workflow_id: "chronicler-audio--agent-session-1",
        phase: "generating_audio" as const,
        detail: "Generating approved audio."
      },
      result: null,
      receipts: [],
      pending_destination_revision: null
    };
    setChroniclerAudioPresentation({
      snapshot,
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
        toolPresentation: chroniclerToolPresentation
      }
    });

    expect(body).toMatch(/activity-row[^]*generation-card/);
    expect(body.match(/class="generation-card/g)).toHaveLength(1);
  });

  it("waits for the current generate_audio row before attributing its generation", () => {
    const snapshot = {
      child_workflow_id: "chronicler-audio--agent-session-1",
      state: "running" as const,
      status: {
        generation_id: "generation-2",
        child_workflow_id: "chronicler-audio--agent-session-1",
        phase: "generating_audio" as const,
        detail: "Generating current audio."
      },
      result: null,
      receipts: [],
      pending_destination_revision: null
    };
    const log = (turnNumber: number, toolId: string, generationId: string) => ({
      id: `${toolId}-start`,
      index: turnNumber,
      ordinal: turnNumber,
      turnNumber,
      sourceTurnNumber: turnNumber,
      turnId: `turn-${turnNumber}`,
      timestamp: turnNumber,
      event: "tool_start" as const,
      actor: "tool" as const,
      tone: "tool" as const,
      label: "Tool started",
      body: "generate_audio",
      toolId,
      toolName: "generate_audio",
      input: { generation_id: generationId },
      status: "running",
      citations: []
    });
    setChroniclerAudioPresentation({
      snapshot,
      generationId: "generation-2",
      toolId: "tool-2",
      cancellation: { enabled: false, detail: "Standby" },
      destinationApproval: null,
      destinationAuthority: { ready: false, detail: "Unavailable" }
    });

    const props = {
        items: [
          { kind: "user" as const, id: "user-1", turnNumber: 1, text: "First", timestamp: 1 },
          { kind: "user" as const, id: "user-2", turnNumber: 2, text: "Second", timestamp: 2 }
        ],
        agentLabel: "Chronicler",
        sessionId: "agent-session-1",
        toolPresentation: chroniclerToolPresentation
    };
    const waiting = render(AgentChatPanel, {
      props: { ...props, logs: [log(1, "tool-1", "generation-1")] }
    }).body;
    const { body } = render(AgentChatPanel, {
      props: {
        ...props,
        logs: [log(1, "tool-1", "generation-1"), log(2, "tool-2", "generation-2")]
      }
    });

    expect(waiting).not.toContain('class="generation-card');
    expect(waiting).not.toContain('class="activity-feed expanded');
    expect(body.match(/class="generation-card/g)).toHaveLength(1);
    expect(body.match(/class="activity-feed expanded/g)).toHaveLength(1);
    expect(body).toMatch(/data-activity-id="tool-2-start"[^]*generation-card/);
  });
});
