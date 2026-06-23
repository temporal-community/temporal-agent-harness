import type { AgentSseFrame, FileCitationAnnotation } from "$lib/api/types";

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

function renderUserMessage(value: string): string {
  if (!value.startsWith("{")) return value;
  try {
    const message = JSON.parse(value) as {
      type?: string;
      payload?: { name?: string; arg?: string; text?: string };
      script?: string;
    };
    if (typeof message.payload?.text === "string") return message.payload.text;
    if (typeof message.script === "string") return message.script;
    if (message.type !== "slash_command" || !message.payload?.name) return value;
    return `/${message.payload.name}${message.payload.arg ? ` ${message.payload.arg}` : ""}`;
  } catch {
    return value;
  }
}

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
  const replyIndexByTurn = new Map<number, number>();
  const toolIndexById = new Map<string, number>();
  const citationsByTurn = new Map<number, FileCitationAnnotation[]>();

  for (const frame of frames) {
    if (!("type" in frame.data)) continue;
    const { turn_number, timestamp } = frame.data;

    if (frame.event === "turn_started") {
      items.push({
        kind: "user",
        id: `user-${frame.data.turn_id}`,
        turnNumber: turn_number,
        text: renderUserMessage(frame.data.user_message),
        timestamp
      });
    }

    if (frame.event === "text_annotation") {
      const existing = citationsByTurn.get(turn_number) ?? [];
      citationsByTurn.set(turn_number, [...existing, ...citationAnnotations(frame)]);
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
          id: `thought-${frame.data.turn_id}-${frame.data.timestamp}`,
          turnNumber: turn_number,
          text,
          timestamp
        });
      }
    }

    if (frame.event === "reply_delta") {
      let itemIndex = replyIndexByTurn.get(turn_number);
      if (itemIndex == null) {
        itemIndex = items.length;
        replyIndexByTurn.set(turn_number, itemIndex);
        items.push({
          kind: "agent",
          id: `reply-${frame.data.turn_id}`,
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

    if (frame.event === "reply") {
      const text = textFromReply(frame.data);
      let itemIndex = replyIndexByTurn.get(turn_number);
      if (itemIndex == null) {
        itemIndex = items.length;
        replyIndexByTurn.set(turn_number, itemIndex);
        items.push({
          kind: "agent",
          id: `reply-${frame.data.turn_id}`,
          turnNumber: turn_number,
          text,
          streaming: false,
          timestamp,
          citations: citationsByTurn.get(turn_number) ?? []
        });
      } else {
        const item = items[itemIndex];
        if (item?.kind === "agent") {
          item.text = text || item.text;
          item.streaming = false;
          item.citations = citationsByTurn.get(turn_number) ?? [];
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
