import type { AgentInterfaceFunction, AgentSseFrame, FileCitationAnnotation } from "$lib/api/types";
import { handlerReply, outputSchemaTracker, streamingIsData } from "$lib/state/handlerReply";
import { messageKey, promptField, renderUserMessage } from "$lib/state/inboundMessageText";
import { thoughtDeltaText } from "$lib/state/thoughtSummary";

export type TranscriptItem =
  | {
      kind: "user";
      id: string;
      turnNumber: number;
      text: string;
      /** The payload field the text came from (`text`, `script`, …), or null when it was labelled. */
      field: string | null;
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
      /** Shown as JSON: the handler is declared as data, or its reply is. */
      data: boolean;
      /** The handler's whole output when it is data, once the reply has ended. */
      json?: unknown;
      /** The handler failed; whatever it streamed first stays above this. */
      error?: string;
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

/** A bare frame is the root's; a timeline entry says whose it is, and only the root's are read. */
type TranscriptInput = AgentSseFrame | { frame: AgentSseFrame; role?: string };

export function buildTranscript(
  frames: readonly TranscriptInput[],
  agentInterface?: AgentInterfaceFunction[]
): TranscriptItem[] {
  return transcriptBuilder()(frames, agentInterface);
}

/**
 * The transcript, folded forward while the frames only grow (same rule as
 * replayTimelineBuilder). An item a new frame updates is copied first, so items handed
 * out earlier never change: a keyed each skips an item whose object is the same.
 */
export function transcriptBuilder(): (
  frames: readonly TranscriptInput[],
  agentInterface?: AgentInterfaceFunction[]
) => TranscriptItem[] {
  let seen = 0;
  let last: TranscriptInput | undefined;
  let iface: AgentInterfaceFunction[] | undefined;
  let items: TranscriptItem[] = [];
  let schemaOf = outputSchemaTracker();
  // Keyed by MESSAGE, not by turn. A turn is refcounted, so two `mid_turn: "accept"` handlers
  // can be streaming under one turn_id at once — keying by turn would merge their replies into
  // one bubble and interleave their text.
  let replyIndexByMessage = new Map<string, number>();
  let toolIndexById = new Map<string, number>();
  let citationsByMessage = new Map<string, FileCitationAnnotation[]>();

  return (frames, agentInterface) => {
    if (agentInterface !== iface || frames.length < seen || (seen > 0 && frames[seen - 1] !== last)) {
      seen = 0;
      iface = agentInterface;
      items = [];
      schemaOf = outputSchemaTracker();
      replyIndexByMessage = new Map();
      toolIndexById = new Map();
      citationsByMessage = new Map();
    }
    const fresh = items.length;
    /* Copy an item made by an earlier call before the first edit in this one. */
    const copied = new Set<number>();
    const editable = (index: number | undefined): TranscriptItem | undefined => {
      if (index == null || items[index] == null) return undefined;
      if (index < fresh && !copied.has(index)) {
        items[index] = { ...items[index]! };
        copied.add(index);
      }
      return items[index];
    };
    const foldFrame = (frame: AgentSseFrame): void => {
      if (!("type" in frame.data)) return;
      const { turn_number, timestamp } = frame.data;
      const key = messageKey(frame);
      const schema = schemaOf(frame, agentInterface);

      if (frame.event === "message_accepted") {
        items.push({
          kind: "user",
          id: `user-${key}`,
          turnNumber: turn_number,
          text: renderUserMessage(frame.data.handler, frame.data.payload),
          field: promptField(frame.data.payload),
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
            citations: [],
            data: false
          });
        }
        const item = editable(itemIndex);
        if (item?.kind === "agent") {
          item.text += frame.data.text;
          item.data = streamingIsData(item.text, schema);
        }
      }

      if (frame.event === "message_handler_error") {
        const item = editable(replyIndexByMessage.get(key));
        if (item?.kind === "agent") {
          item.streaming = false;
          item.error = frame.data.message;
        }
      }

      if (frame.event === "message_handler_end") {
        const { text, json } = handlerReply(frame.data, schema);
        const data = json !== undefined;
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
            citations: citationsByMessage.get(key) ?? [],
            data,
            json
          });
        } else {
          const item = editable(itemIndex);
          if (item?.kind === "agent") {
            item.text = text || item.text;
            item.streaming = false;
            item.citations = citationsByMessage.get(key) ?? [];
            item.data = data;
            item.json = json;
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
        const item = editable(itemIndex);
        if (!item || item.kind !== "tool") return;
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
          /* The evaluator was canceled because the gate was settled first. Nothing to say
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
    };
    for (let position = seen; position < frames.length; position += 1) {
      const item = frames[position]!;
      if (!("frame" in item)) foldFrame(item);
      else if (item.role !== "subagent") foldFrame(item.frame);
    }
    seen = frames.length;
    last = frames[seen - 1];
    return items.slice();
  };
}
