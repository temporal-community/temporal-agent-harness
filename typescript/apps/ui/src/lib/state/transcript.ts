import type { AgentInterfaceFunction, AgentSseFrame, FileCitationAnnotation } from "$lib/api/types";
import { handlerReply, outputSchemaTracker, streamingIsData } from "$lib/state/handlerReply";
import { messageKey, promptField, renderUserMessage } from "$lib/state/inboundMessageText";
import { thoughtDeltaText } from "$lib/state/thoughtSummary";
import { toolOutputText } from "$lib/state/toolOutput";

export type TranscriptItem =
  | {
      kind: "user";
      id: string;
      turnNumber: number;
      /** The handler the message went to. */
      handler: string;
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

type TranscriptFold = ((
  frames: readonly TranscriptInput[],
  agentInterface?: AgentInterfaceFunction[]
) => TranscriptItem[]) & {
  /** `buildTranscript(frames.slice(0, cursor))` for the frames last folded, cut from that result. */
  at(cursor: number): TranscriptItem[];
};

/** Kept until the fold starts over. */
interface CutRun {
  /** An item the cursor sits inside, folded from its own frames, by how many it has read. */
  partial: Map<number, { read: number; item: TranscriptItem; fold: TranscriptFold; frames: TranscriptInput[] }>;
  previous: TranscriptItem[];
}

/* The frames a reply is folded from: the handler its message went to decides prose or
   data, and its annotations become citations when it ends. */
const MESSAGE_SOURCES = new Set<string>([
  "message_accepted",
  "text_annotation",
  "reply_delta",
  "message_handler_error",
  "message_handler_end"
]);
const NO_SOURCES: number[] = [];

const countBelow = (sorted: readonly number[], limit: number, end = sorted.length): number => {
  let low = 0;
  let high = end;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (sorted[middle]! < limit) low = middle + 1;
    else high = middle;
  }
  return low;
};

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
export function transcriptBuilder(): TranscriptFold {
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
  let from: number[] = [];
  let changed: number[] = [];
  let sources: number[][] = [];
  let sourcesByMessage = new Map<string, number[]>();
  let sourcesByTool = new Map<string, number[]>();
  let run: CutRun = { partial: new Map(), previous: [] };
  let input: readonly TranscriptInput[] = [];
  let result: TranscriptItem[] = [];

  const fold = ((frames, agentInterface) => {
    if (agentInterface !== iface || frames.length < seen || (seen > 0 && frames[seen - 1] !== last)) {
      seen = 0;
      iface = agentInterface;
      items = [];
      schemaOf = outputSchemaTracker();
      replyIndexByMessage = new Map();
      toolIndexById = new Map();
      citationsByMessage = new Map();
      from = [];
      changed = [];
      sources = [];
      sourcesByMessage = new Map();
      sourcesByTool = new Map();
      run = { partial: new Map(), previous: [] };
    }
    const fresh = items.length;
    let position = seen;
    /* Copy an item made by an earlier call before the first edit in this one. */
    const copied = new Set<number>();
    const editable = (index: number | undefined): TranscriptItem | undefined => {
      if (index == null || items[index] == null) return undefined;
      if (index < fresh && !copied.has(index)) {
        items[index] = { ...items[index]! };
        copied.add(index);
      }
      changed[index] = position;
      return items[index];
    };
    const sourcesOf = (byId: Map<string, number[]>, id: string): number[] => {
      let list = byId.get(id);
      if (!list) byId.set(id, (list = []));
      list.push(position);
      return list;
    };
    const opened = (list: number[]): void => {
      from.push(position);
      changed.push(position);
      sources.push(list);
    };
    const foldFrame = (frame: AgentSseFrame): void => {
      if (!("type" in frame.data)) return;
      const { turn_number, timestamp } = frame.data;
      const key = messageKey(frame);
      const schema = schemaOf(frame, agentInterface);
      if (MESSAGE_SOURCES.has(frame.event)) sourcesOf(sourcesByMessage, key);

      if (frame.event === "message_accepted") {
        opened(NO_SOURCES);
        items.push({
          kind: "user",
          id: `user-${key}`,
          turnNumber: turn_number,
          handler: frame.data.handler,
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
          opened(NO_SOURCES);
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
          opened(sourcesByMessage.get(key)!);
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
          opened(sourcesByMessage.get(key)!);
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
        const toolSources = sourcesOf(sourcesByTool, toolId);
        let itemIndex = toolIndexById.get(toolId);
        if (itemIndex == null) {
          itemIndex = items.length;
          toolIndexById.set(toolId, itemIndex);
          opened(toolSources);
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
          item.output = toolOutputText(frame.data.tool_output);
        } else if (frame.event === "tool_error") {
          item.status = "failed";
          item.message = frame.data.message;
        }
      }
    };
    for (; position < frames.length; position += 1) {
      const item = frames[position]!;
      if (!("frame" in item)) foldFrame(item);
      else if (item.role !== "subagent") foldFrame(item.frame);
    }
    seen = frames.length;
    last = frames[seen - 1];
    input = frames;
    return (result = items.slice());
  }) as TranscriptFold;

  /**
   * `buildTranscript(input.slice(0, cursor))`, cut from the transcript folded over all of
   * `input` instead of rebuilt from frame one.
   *
   * Items are in the order their first frame arrived, so the cut is a prefix. An item no
   * frame at or past the cursor changed is the full transcript's own object; only the few
   * the cursor sits inside (a reply still streaming, a tool still running) are folded again,
   * from their own frames. Those are kept by how far they have read, so a step that leaves
   * them alone hands back the same objects, and a cut equal to the last one is that array.
   */
  fold.at = (cursor) => {
    const full = result;
    const end = Math.min(Math.max(cursor, 0), input.length);
    const kept = countBelow(from, end, full.length);
    const cut = full.slice(0, kept);
    for (let index = 0; index < kept; index += 1) {
      if (changed[index]! < end) continue;
      const positions = sources[index]!;
      const read = countBelow(positions, end);
      let partial = run.partial.get(index);
      if (partial?.read !== read) {
        partial ??= { read: 0, item: full[index]!, fold: transcriptBuilder(), frames: [] };
        while (partial.frames.length < read) partial.frames.push(input[positions[partial.frames.length]!]!);
        const id = full[index]!.id;
        partial.item = partial.fold(partial.frames.slice(0, read), iface).find((item) => item.id === id)!;
        partial.read = read;
        run.partial.set(index, partial);
      }
      cut[index] = partial.item;
    }
    const previous = run.previous;
    if (previous.length === cut.length && cut.every((item, index) => item === previous[index])) return previous;
    return (run.previous = cut);
  };
  return fold;
}
