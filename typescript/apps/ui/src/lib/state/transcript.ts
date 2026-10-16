import type { AgentSseFrame, FileCitationAnnotation } from "$lib/api/types";
import { messageKey, renderUserMessage } from "$lib/state/inboundMessageText";
import { thoughtDeltaText } from "$lib/state/thoughtSummary";

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
      /** "evaluating": an automatic approval check is deciding right now. Distinct from
       *  "awaiting", which means a PERSON has to act — the pending gate alone cannot tell
       *  those apart, and they call for completely different UI. */
      status:
        | "requested"
        | "awaiting"
        | "evaluating"
        | "approved"
        | "running"
        | "done"
        | "failed"
        | "denied";
      /** Absent means no `tool_input` on the frame; `null` means the frame carried one and it
       *  was unknown (arguments streamed but unparseable), which is not the same as `{}`. */
      input?: Record<string, unknown> | null;
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
      const text = thoughtDeltaText(frame.data.delta);
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
      frame.event === "auto_approval_evaluation_started" ||
      frame.event === "auto_approval_evaluation_ended" ||
      frame.event === "auto_approval_evaluation_superseded" ||
      frame.event === "auto_approval_evaluation_error" ||
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
      else if (frame.event === "auto_approval_evaluation_started") {
        item.status = "evaluating";
        item.message = `${frame.data.evaluator} is deciding…`;
      } else if (frame.event === "auto_approval_evaluation_ended") {
        /* An approve or a deny is about to arrive as its own tool_approval_resolved, which
           overwrites this. An ESCALATE is not — it resolves nothing — so for that verdict
           this is the row's only explanation of why it is now sitting with a human. */
        item.status = "awaiting";
        item.message = frame.data.reason ?? `${frame.data.evaluator}: ${frame.data.verdict}`;
      } else if (frame.event === "auto_approval_evaluation_superseded") {
        /* The evaluator was cancelled because the gate was settled first. Nothing to say
           on the tool row — the resolution that beat it is the next frame and speaks for
           itself; the cancellation is on the evaluation's own node and log row. */
        item.status = item.status === "evaluating" ? "awaiting" : item.status;
      } else if (frame.event === "auto_approval_evaluation_error") {
        item.status = "awaiting";
        item.message = `${frame.data.evaluator} failed: ${frame.data.message}`;
      } else if (frame.event === "tool_approval_resolved") {
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
