import type { AgentSseFrame, FileCitationAnnotation } from "$lib/api/types";
import { messageKey, renderUserMessage } from "./userMessage";

export type TranscriptItem =
  | {
      kind: "user";
      id: string;
      turnNumber: number;
      text: string;
      timestamp: number;
    }
  | {
      kind: "agent";
      id: string;
      turnNumber: number;
      text: string;
      streaming: boolean;
      timestamp: number;
      citations: FileCitationAnnotation[];
    }
  | {
      kind: "tool";
      id: string;
      turnNumber: number;
      toolId: string;
      toolName: string;
      status: "requested" | "awaiting" | "approved" | "running" | "done" | "failed" | "denied";
      input?: Record<string, unknown>;
      output?: string;
      message?: string;
      timestamp: number;
    }
  | {
      kind: "thought";
      id: string;
      turnNumber: number;
      text: string;
      timestamp: number;
    };

function textFromReply(data: { text?: unknown; output?: unknown }): string {
  if (typeof data.text === "string") return data.text;
  const output = data.output;
  if (typeof output === "string") return output;
  if (typeof output === "object" && output != null) {
    if ("text" in output && typeof output.text === "string") return output.text;
    if ("message" in output && typeof output.message === "string") return output.message;
  }
  return "";
}

function citationAnnotations(frame: AgentSseFrame): FileCitationAnnotation[] {
  if (frame.event !== "text_annotation" || !("type" in frame.data)) return [];
  return (frame.data.delta.annotations ?? []).filter(
    (item): item is FileCitationAnnotation =>
      typeof item === "object" &&
      item != null &&
      "type" in item &&
      item.type === "file_citation"
  );
}

export function buildTranscript(frames: AgentSseFrame[]): TranscriptItem[] {
  const items: TranscriptItem[] = [];
  // Keyed by MESSAGE, not by turn. A turn is refcounted, so two `mid_turn: "accept"` handlers
  // can be streaming under one turn_id at once — keying by turn would merge their replies into
  // one bubble and interleave their text.
  const replyIndexByMessage = new Map<string, number>();
  const toolIndexById = new Map<string, number>();
  const citationsByMessage = new Map<string, FileCitationAnnotation[]>();

  for (const frame of frames) {
    if (!("type" in frame.data)) continue;
    const { turn_number, timestamp } = frame.data;
    const key = messageKey(frame);

    if (frame.event === "message_accepted") {
      items.push({
        kind: "user",
        id: `user-${key}`,
        turnNumber: turn_number,
        text: renderUserMessage(frame.data.handler, frame.data.payload),
        timestamp
      });
    }

    if (frame.event === "text_annotation") {
      const existing = citationsByMessage.get(key) ?? [];
      citationsByMessage.set(key, [...existing, ...citationAnnotations(frame)]);
    }

    if (frame.event === "thought_summary") {
      const content = frame.data.delta.content;
      const text =
        typeof content === "object" && content != null && "text" in content
          ? String((content as { text?: unknown }).text ?? "")
          : "";
      if (text) {
        items.push({
          kind: "thought",
          id: `thought-${key}-${frame.data.timestamp}`,
          turnNumber: turn_number,
          text,
          timestamp
        });
      }
    }

    if (frame.event === "reply_delta") {
      let itemIndex = replyIndexByMessage.get(key);
      if (itemIndex == null) {
        itemIndex = items.length;
        replyIndexByMessage.set(key, itemIndex);
        items.push({
          kind: "agent",
          id: `reply-${key}`,
          turnNumber: turn_number,
          text: "",
          streaming: true,
          timestamp,
          citations: []
        });
      }
      const item = items[itemIndex];
      if (item?.kind === "agent") item.text += frame.data.text;
    }

    if (frame.event === "message_handler_end") {
      const text = textFromReply(frame.data);
      let itemIndex = replyIndexByMessage.get(key);
      if (itemIndex == null) {
        itemIndex = items.length;
        replyIndexByMessage.set(key, itemIndex);
        items.push({
          kind: "agent",
          id: `reply-${key}`,
          turnNumber: turn_number,
          text,
          streaming: false,
          timestamp,
          citations: citationsByMessage.get(key) ?? []
        });
      } else {
        const item = items[itemIndex];
        if (item?.kind === "agent") {
          item.text = text || item.text;
          item.streaming = false;
          item.citations = citationsByMessage.get(key) ?? [];
        }
      }
    }

    if (
      frame.event === "tool_requested" ||
      frame.event === "tool_approval_requested" ||
      frame.event === "tool_approval_resolved" ||
      frame.event === "tool_start" ||
      frame.event === "tool_progress_delta" ||
      frame.event === "tool_end" ||
      frame.event === "tool_error"
    ) {
      const toolId = frame.data.tool_id;
      let itemIndex = toolIndexById.get(toolId);
      if (itemIndex == null) {
        itemIndex = items.length;
        toolIndexById.set(toolId, itemIndex);
        items.push({
          kind: "tool",
          id: `tool-${toolId}`,
          turnNumber: turn_number,
          toolId,
          toolName: frame.data.tool_name,
          status: "requested",
          timestamp
        });
      }
      const item = items[itemIndex];
      if (!item || item.kind !== "tool") continue;
      item.timestamp = timestamp;
      if ("tool_input" in frame.data) item.input = frame.data.tool_input;
      if (frame.event === "tool_approval_requested") item.status = "awaiting";
      else if (frame.event === "tool_approval_resolved") {
        item.status = frame.data.approved ? "approved" : "denied";
        item.message = frame.data.reason ?? undefined;
      } else if (frame.event === "tool_start") item.status = "running";
      else if (frame.event === "tool_progress_delta") {
        item.status = "running";
        item.message = frame.data.progress_delta;
      }
      else if (frame.event === "tool_end") {
        item.status = "done";
        item.output = frame.data.tool_output;
      } else if (frame.event === "tool_error") {
        item.status = "failed";
        item.message = frame.data.message;
      }
    }
  }

  return items;
}
