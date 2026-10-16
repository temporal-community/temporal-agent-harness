import {
  SYNTHESIZED,
  type AgentEventType,
  type AgentSseFrame,
  type FileCitationAnnotation,
  type JsonPatchOp,
  type JsonRecord,
  type ToolId
} from "$lib/api/types";
import { formatTokens, summarizeCost, type UsageTotals } from "$lib/cost/pricing";
import { renderUserMessage } from "$lib/state/inboundMessageText";
import { HISTORY_GAP_NOTE, findHistoryGaps } from "$lib/state/historyGap";
import { buildReplyRuns, type ReplyRun } from "$lib/state/replyRuns";
import { thoughtDeltaText } from "$lib/state/thoughtSummary";

export type ReplayActor =
  | "user"
  | "agent"
  | "model"
  | "tool"
  | "approval"
  | "queue"
  | "reasoning"
  | "subagent"
  | "system"
  | "error";

export type ReplayTone =
  | "neutral"
  | "agent"
  | "model"
  | "tool"
  | "approval"
  | "done"
  | "error"
  | "queue";

export type ReplayMarkerTone = "approval" | "error" | "queue";

export interface ReplayLogRow {
  id: string;
  index: number;
  /**
   * Set only on a row standing for a RUN of frames — a collapsed reply stream
   * (see replyRuns.ts). It is where the run opens; `index` is its last frame, so
   * the row is addressed at the state AFTER the whole run. `id` and `ordinal`
   * stay at the opening frame, so a run still streaming keeps its identity while
   * `index` and `body` grow with it.
   */
  runStartIndex?: number;
  ordinal: number;
  turnNumber: number;
  sourceTurnNumber: number;
  parentTurnNumber?: number;
  workflowId?: string;
  /** A subagent's workflow: the one that published this row, or the one a subagent_* event is about. */
  childWorkflowId?: string;
  sourceLabel?: string;
  turnId: string;
  messageId?: string;
  evaluationId?: string;
  eventOffset?: number;
  timestamp: number;
  // An agent event's own type, or "stream_error" for the client-side frame /api/chat
  // synthesizes — which no agent published, so it is not an AgentEventType.
  event: AgentEventType | "stream_error";
  actor: ReplayActor;
  tone: ReplayTone;
  label: string;
  status?: string;
  body?: string;
  detail?: string;
  model?: string | null;
  toolId?: ToolId;
  toolName?: string;
  /** Absent means the frame carried no `tool_input`; `null` means it carried one and it was
   *  unknown (arguments streamed but unparseable), which is not the same as `{}`. The two
   *  render differently — see formatLogValue in $lib/state/logValue. */
  input?: JsonRecord | null;
  output?: string;
  /** JSON schema of the result a `callback_requested` row is waiting on. */
  outputSchema?: JsonRecord;
  citations: FileCitationAnnotation[];
  usage?: UsageTotals;
  estimatedCostUsd?: number | null;
  marker?: ReplayMarkerTone;
  markerLabel?: string;
  /**
   * The run's history is discontinuous immediately BEFORE this row — set to
   * HISTORY_GAP_NOTE, which is the whole of what is known (see historyGap.ts).
   *
   * On the row after the seam rather than the one before it, because the seam is
   * read going forwards: "what follows is not continuous with what precedes it"
   * is a statement about this row, and the row before it is a complete event that
   * nothing is wrong with.
   */
  gapBefore?: string;
}

export interface TurnLogSummary {
  turnNumber: number;
  startedAt: number;
  endedAt: number;
  durationSeconds: number;
  preview: string;
  eventCount: number;
  modelCalls: number;
  toolCalls: number;
  approvals: number;
  errors: number;
  tokens: number;
  estimatedCostUsd: number | null;
}

export interface TurnLogGroup {
  turnNumber: number;
  startedAt: number;
  rows: ReplayLogRow[];
  summary: TurnLogSummary;
}

export interface ReplayLog {
  rows: ReplayLogRow[];
  groups: TurnLogGroup[];
}

export interface ReplayMarker {
  id: string;
  index: number;
  turnNumber: number;
  tone: ReplayMarkerTone;
  label: string;
}

export interface ReplayLogFrame {
  frame: AgentSseFrame;
  workflowId?: string;
  role?: "parent" | "subagent";
  label?: string;
  parentTurnNumber?: number;
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

function citationBody(citations: FileCitationAnnotation[]): string {
  if (!citations.length) return "Citation metadata attached.";
  return citations
    .map(
      (citation) =>
        citation.custom_metadata?.heading ??
        citation.file_name ??
        citation.document_uri ??
        "Source"
    )
    .join(", ");
}

/**
 * Which paths one state commit touched, short enough for a log row.
 *
 * The paths, not the count: a commit's ops are the whole of what it did, and
 * "4 ops" says only that something happened. Three of them is enough to tell two
 * commits apart at a glance; the state pane has the rest, with the values.
 */
function stateOpsBody(ops: JsonPatchOp[]): string {
  const shown = ops.slice(0, 3).map((op) => `${op.op} ${op.path || "/"}`);
  const rest = ops.length - shown.length;
  return rest > 0 ? `${shown.join(", ")}, +${rest} more` : shown.join(", ");
}

function modelUsageBody(usage: UsageTotals): string {
  return `${formatTokens(usage.total)} tokens`;
}

function normalizeReplayLogFrame(item: AgentSseFrame | ReplayLogFrame): ReplayLogFrame {
  return "frame" in item ? item : { frame: item, role: "parent" };
}

function rowFromFrame(
  entry: ReplayLogFrame,
  frameIndex: number
): ReplayLogRow | null {
  const { frame } = entry;
  const ordinal = frameIndex + 1;
  if (!("type" in frame.data)) {
    return {
      id: `${entry.workflowId ?? "parent"}-stream-error-${ordinal}`,
      index: ordinal,
      ordinal,
      turnNumber: 0,
      sourceTurnNumber: 0,
      workflowId: entry.workflowId,
      sourceLabel: entry.label,
      turnId: "client",
      timestamp: 0,
      event: "stream_error",
      actor: "error",
      tone: "error",
      label: "Stream error",
      body: frame.data.message,
      citations: [],
      marker: "error",
      markerLabel: "stream error"
    };
  }

  const sourceTurnNumber = frame.data.turn_number;
  const turnNumber =
    entry.role === "subagent" && entry.parentTurnNumber != null
      ? entry.parentTurnNumber
      : sourceTurnNumber;
  const base = {
    id: `${entry.workflowId ?? frame.data.agent_id}-${frame.data.type}-${ordinal}`,
    index: ordinal,
    ordinal,
    turnNumber,
    sourceTurnNumber,
    parentTurnNumber: entry.role === "subagent" ? entry.parentTurnNumber : undefined,
    workflowId: entry.workflowId,
    childWorkflowId:
      ("workflow_id" in frame.data ? frame.data.workflow_id : undefined) ??
      (entry.role === "subagent" ? entry.workflowId : undefined),
    sourceLabel: entry.label,
    turnId: frame.data.turn_id,
    messageId: frame.data.message_id ?? undefined,
    evaluationId: "evaluation_id" in frame.data ? frame.data.evaluation_id : undefined,
    eventOffset:
      frame.data.event_offset != null && frame.data.event_offset !== SYNTHESIZED
        ? frame.data.event_offset
        : undefined,
    timestamp: frame.data.timestamp,
    event: frame.data.type,
    citations: [] as FileCitationAnnotation[]
  };

  if (frame.event === "message_accepted") {
    // One row for every admitted message, labelled by what it did to the turn — the queued
    // case is no longer a separate event, and "joined" is a case the old vocabulary could not
    // express at all.
    const queued = frame.data.disposition === "queued";
    return {
      ...base,
      actor: queued ? "queue" : "user",
      tone: "queue",
      label:
        frame.data.disposition === "joined"
          ? "Message joined the open turn"
          : queued
            ? "Message queued"
            : "User message received",
      body: renderUserMessage(frame.data.handler, frame.data.payload),
      marker: queued ? "queue" : undefined,
      markerLabel: queued ? "queued turn" : undefined
    };
  }

  if (frame.event === "turn_started") {
    return {
      ...base,
      actor: "system",
      tone: "neutral",
      label: "Turn started"
    };
  }

  if (frame.event === "message_handler_start") {
    return {
      ...base,
      actor: "system",
      tone: "neutral",
      label: "Handler started"
    };
  }

  if (frame.event === "model_interaction_started") {
    return {
      ...base,
      actor: "model",
      tone: "model",
      label: "Model started",
      body: frame.data.model ?? "unknown model",
      model: frame.data.model,
      status: "running"
    };
  }

  if (frame.event === "model_interaction_ended") {
    const summary = summarizeCost([frame]);
    return {
      ...base,
      actor: "model",
      tone: "done",
      label: "Model completed",
      body: modelUsageBody(summary.tokens),
      model: frame.data.model,
      status: "completed",
      usage: summary.tokens,
      estimatedCostUsd: summary.estimatedCostUsd
    };
  }

  if (frame.event === "tool_requested") {
    return {
      ...base,
      actor: "tool",
      tone: "tool",
      label: "Tool requested",
      body: frame.data.tool_name,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      input: frame.data.tool_input,
      status: "requested"
    };
  }

  if (frame.event === "tool_approval_requested") {
    return {
      ...base,
      actor: "approval",
      tone: "approval",
      label: "Approval requested",
      body: frame.data.tool_name,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      input: frame.data.tool_input,
      status: "awaiting",
      marker: "approval",
      markerLabel: "approval requested"
    };
  }

  if (frame.event === "auto_approval_evaluation_started") {
    return {
      ...base,
      actor: "approval",
      tone: "approval",
      label: "Approval check started",
      body: `${frame.data.evaluator} · ${frame.data.tool_name}`,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      status: "awaiting",
      marker: "approval",
      markerLabel: "approval check"
    };
  }

  if (frame.event === "auto_approval_evaluation_ended") {
    const verdict = frame.data.verdict;
    /* An escalate is not a failure — it is the evaluator correctly declining to decide —
       so it reads as "approval" (still pending a human), not as an error. */
    const tone: ReplayTone =
      verdict === "approve" ? "done" : verdict === "deny" ? "error" : "approval";
    const details = frame.data.details;
    return {
      ...base,
      actor: "approval",
      tone,
      label: `Approval check: ${verdict}`,
      body: frame.data.reason ?? undefined,
      /* The evaluator's structured reasoning, rendered as JSON — this is the whole audit
         record of an automatic decision, and for an escalate it is the only one. */
      detail:
        details && Object.keys(details).length > 0
          ? JSON.stringify(details, null, 2)
          : undefined,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      status: verdict === "approve" ? "approved" : verdict === "deny" ? "denied" : "awaiting"
    };
  }

  if (frame.event === "auto_approval_evaluation_superseded") {
    return {
      ...base,
      actor: "approval",
      /* Neutral, not an error: nothing went wrong — the answer simply arrived from
         somewhere else first, and the evaluator was stopped rather than left running. */
      tone: "neutral",
      label: "Approval check cancelled",
      body: frame.data.verdict
        ? `the gate was decided first; it had reached "${frame.data.verdict}"`
        : "the gate was decided first",
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      status: "superseded"
    };
  }

  if (frame.event === "auto_approval_evaluation_error") {
    return {
      ...base,
      actor: "approval",
      tone: "error",
      label: "Approval check failed",
      body: frame.data.message,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      status: "awaiting",
      marker: "error",
      markerLabel: "approval check failed"
    };
  }

  if (frame.event === "tool_approval_resolved") {
    const approved = frame.data.approved;
    return {
      ...base,
      actor: "approval",
      tone: approved ? "done" : "error",
      label: approved ? "Approval granted" : "Approval denied",
      body: frame.data.reason ?? undefined,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      status: approved ? "approved" : "denied",
      marker: approved ? undefined : "error",
      markerLabel: approved ? undefined : "approval denied"
    };
  }

  if (frame.event === "tool_start") {
    return {
      ...base,
      actor: "tool",
      tone: "tool",
      label: "Tool started",
      body: frame.data.tool_name,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      input: frame.data.tool_input,
      status: "running"
    };
  }

  if (frame.event === "tool_progress_delta") {
    return {
      ...base,
      actor: "tool",
      tone: "tool",
      label: "Tool progress",
      body: frame.data.progress_delta,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      status: "running"
    };
  }

  if (frame.event === "tool_end") {
    return {
      ...base,
      actor: "tool",
      tone: "done",
      label: "Tool completed",
      body: frame.data.tool_name,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      output: frame.data.tool_output,
      status: "done"
    };
  }

  if (frame.event === "tool_error") {
    return {
      ...base,
      actor: "tool",
      tone: "error",
      label: "Tool failed",
      body: frame.data.message,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      status: "failed",
      marker: "error",
      markerLabel: "tool failed"
    };
  }

  if (frame.event === "callback_requested") {
    return {
      ...base,
      actor: "tool",
      tone: "approval",
      label: "Waiting on client",
      body: frame.data.tool_name,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      input: frame.data.tool_input,
      outputSchema: frame.data.output_schema,
      status: "awaiting",
      marker: "approval",
      markerLabel: "callback requested"
    };
  }

  if (frame.event === "callback_resolved") {
    const ok = frame.data.outcome === "ok";
    return {
      ...base,
      actor: "tool",
      tone: ok ? "done" : "error",
      label: ok ? "Client responded" : `Callback ${frame.data.outcome}`,
      body: frame.data.error ?? frame.data.tool_name,
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      status: frame.data.outcome
    };
  }

  if (frame.event === "subagent_started") {
    return {
      ...base,
      actor: "subagent",
      tone: "agent",
      label: "Subagent started",
      body: `${frame.data.agent_key} · ${frame.data.subagent_id}`,
      detail: frame.data.workflow_id,
      status: "running"
    };
  }

  if (frame.event === "subagent_message_sent") {
    return {
      ...base,
      actor: "subagent",
      tone: "tool",
      label: "Subagent message sent",
      body: `${frame.data.handler} → turn ${frame.data.subagent_turn}`,
      detail: `${frame.data.agent_key} · ${frame.data.subagent_id}`,
      status: "dispatched"
    };
  }

  if (frame.event === "subagent_reply_received") {
    return {
      ...base,
      actor: "subagent",
      tone: frame.data.outcome === "ok" ? "done" : "error",
      label: "Subagent reply received",
      body: `${frame.data.handler} → turn ${frame.data.subagent_turn}`,
      detail: `${frame.data.agent_key} · ${frame.data.subagent_id}`,
      status: frame.data.outcome
    };
  }

  if (frame.event === "subagent_stopped") {
    return {
      ...base,
      actor: "subagent",
      tone: "done",
      label: "Subagent stopped",
      body: `${frame.data.agent_key} · ${frame.data.subagent_id}`,
      detail: frame.data.workflow_id,
      status: "stopped"
    };
  }

  if (frame.event === "subagent_stream_unavailable") {
    return {
      ...base,
      actor: "subagent",
      tone: "error",
      label: "Subagent stream unavailable",
      body: frame.data.reason || "Subagent detail was unavailable.",
      detail: frame.data.workflow_id,
      status: "degraded",
      marker: "error",
      markerLabel: "subagent stream unavailable"
    };
  }

  if (frame.event === "thought_summary") {
    return {
      ...base,
      actor: "reasoning",
      tone: "model",
      label: "Reasoning summary",
      body: thoughtDeltaText(frame.data.delta)
    };
  }

  if (frame.event === "reply_delta") {
    return {
      ...base,
      actor: "agent",
      tone: "agent",
      label: "Reply streaming",
      body: frame.data.text,
      status: "streaming"
    };
  }

  if (frame.event === "text_annotation") {
    const citations = citationAnnotations(frame);
    return {
      ...base,
      actor: "agent",
      tone: "agent",
      label: "Citation attached",
      body: citationBody(citations),
      citations
    };
  }

  if (frame.event === "message_handler_end") {
    return {
      ...base,
      actor: "agent",
      tone: "done",
      label: "Handler reply",
      body: textFromReply(frame.data),
      status: "complete"
    };
  }

  if (frame.event === "turn_end") {
    return {
      ...base,
      actor: "system",
      tone: "neutral",
      label: "Turn ended",
      status: "idle"
    };
  }

  if (frame.event === "message_handler_error") {
    return {
      ...base,
      actor: "error",
      tone: "error",
      label: "Handler error",
      body: frame.data.message,
      marker: "error",
      markerLabel: "handler error"
    };
  }

  /* State rides this stream rather than a topic of its own precisely so that a
     commit is ORDERED against the tool call and the reply delta around it. Rows
     here are what cashes that in: leave them out and the log quietly asserts
     that nothing happened between two model calls. */
  if (frame.event === "state_snapshot") {
    return {
      ...base,
      actor: "system",
      tone: "neutral",
      label: "State registered",
      body: frame.data.state_id,
      status: `v${frame.data.version}`
    };
  }

  if (frame.event === "state_patch") {
    return {
      ...base,
      actor: "system",
      tone: "neutral",
      label: "State changed",
      body: `${frame.data.state_id} — ${stateOpsBody(frame.data.ops ?? [])}`,
      status: `v${frame.data.version}`
    };
  }

  return null;
}

function buildSummary(turnNumber: number, rows: ReplayLogRow[]): TurnLogSummary {
  const startedAt = Math.min(...rows.map((row) => row.timestamp));
  const endedAt = Math.max(...rows.map((row) => row.timestamp));
  const toolIds = new Set(rows.map((row) => row.toolId).filter(Boolean));
  const estimatedCostUsd = rows.every((row) => row.estimatedCostUsd !== null)
    ? rows.reduce((sum, row) => sum + (row.estimatedCostUsd ?? 0), 0)
    : null;

  return {
    turnNumber,
    startedAt,
    endedAt,
    durationSeconds: endedAt - startedAt,
    preview: rows.find((row) => row.actor === "user")?.body ?? "No user message",
    eventCount: rows.length,
    modelCalls: rows.filter((row) => row.event === "model_interaction_ended").length,
    toolCalls: toolIds.size,
    approvals: rows.filter((row) => row.event === "tool_approval_requested").length,
    errors: rows.filter((row) => row.tone === "error").length,
    tokens: rows.reduce((sum, row) => sum + (row.usage?.total ?? 0), 0),
    estimatedCostUsd
  };
}

export function buildReplayLog(input: Array<AgentSseFrame | ReplayLogFrame>): ReplayLog {
  const gapPositions = findHistoryGaps(input);
  /* A run of reply chunks draws ONE row, carrying all of their text. A lone chunk
     is left exactly as it was — it is already one event, and a row that announced
     itself as a collapsed run of one would only be noise. */
  const runStarts = new Map<number, ReplyRun>();
  const withinRun = new Set<number>();
  for (const run of buildReplyRuns(input)) {
    if (run.frameCount < 2) continue;
    runStarts.set(run.startIndex, run);
    for (let index = run.startIndex + 1; index <= run.endIndex; index += 1) {
      withinRun.add(index);
    }
  }
  /* Carried forward for the same reason the waterfall carries it: `rowFromFrame`
     answers null for an event kind this log does not render, and a seam attached to
     a frame that draws no row would never be seen. A chunk folded into the row above
     is the same case — the seam waits for the next row that can carry it. */
  let pendingGap = false;
  const rows: ReplayLogRow[] = [];
  input.forEach((item, index) => {
    if (gapPositions.has(index)) pendingGap = true;
    if (withinRun.has(index + 1)) return;
    const row = rowFromFrame(normalizeReplayLogFrame(item), index);
    if (!row) return;
    const run = runStarts.get(row.index);
    if (run) {
      row.runStartIndex = run.startIndex;
      row.index = run.endIndex;
      row.body = run.text;
    }
    if (pendingGap) {
      row.gapBefore = HISTORY_GAP_NOTE;
      pendingGap = false;
    }
    rows.push(row);
  });

  const groupedRows = new Map<number, ReplayLogRow[]>();
  for (const row of rows) {
    const current = groupedRows.get(row.turnNumber) ?? [];
    current.push(row);
    groupedRows.set(row.turnNumber, current);
  }

  const groups = [...groupedRows.entries()]
    .filter(([turnNumber]) => turnNumber > 0)
    .map(([turnNumber, rows]) => ({
      turnNumber,
      startedAt: Math.min(...rows.map((row) => row.timestamp)),
      rows,
      summary: buildSummary(turnNumber, rows)
    }));

  return { rows, groups };
}

/**
 * Whether a row is the one standing for the frame at a 1-based index.
 *
 * Exact for an ordinary row. A collapsed run answers for every frame it folded,
 * so a cursor parked anywhere inside a streamed reply — by scrubbing the lane,
 * which is free-form where the step keys are not — still reads as that one event
 * rather than as nothing at all.
 */
export function rowCovers(row: ReplayLogRow, index: number): boolean {
  if (row.runStartIndex == null) return row.index === index;
  return index >= row.runStartIndex && index <= row.index;
}

export function buildReplayMarkers(log: ReplayLog): ReplayMarker[] {
  return log.rows
    .filter((row) => row.marker)
    .map((row) => ({
      id: `marker-${row.ordinal}`,
      index: row.index,
      turnNumber: row.turnNumber,
      tone: row.marker ?? "queue",
      label: row.markerLabel ?? row.label
    }));
}

/* Most statuses restate the label they sit next to ("Tool completed" carries
   status "done"), so a status has to survive these stems before it is worth
   showing. Unknown values like a subagent's "timeout" fall through and stay. */
const IMPLIED_STATUS_STEMS: Record<string, string[]> = {
  running: ["start", "progress", "stream"],
  done: ["complet", "final"],
  complete: ["complet", "final"],
  idle: ["end"],
  dispatched: ["sent"],
  approved: ["grant"],
  awaiting: ["request"],
  degraded: ["unavailable"]
};

/** The row's status, or null when the row's label already carries it. */
export function statusNote(row: ReplayLogRow): string | null {
  const status = row.status?.trim();
  if (!status) return null;
  const label = row.label.toLowerCase();
  const value = status.toLowerCase();
  if (label.includes(value)) return null;
  if ((IMPLIED_STATUS_STEMS[value] ?? []).some((stem) => label.includes(stem))) return null;
  return status;
}

export interface RowIdentifier {
  label: string;
  value: string;
}

/**
 * The IDs that tell this row apart, most useful first. The session's own workflow
 * ID is left out on purpose: every row shares it, and the session anchor already
 * shows it. A turn or message ID is only shown on the row that event is about,
 * or it would repeat down the whole turn the same way.
 */
export function rowIdentifiers(row: ReplayLogRow): RowIdentifier[] {
  const turnRow = row.event === "turn_started" || row.event === "turn_end";
  const messageRow = row.event.startsWith("message_");
  const candidates: Array<[string, string | undefined]> = [
    ["tool call", row.toolId],
    ["evaluation", row.evaluationId],
    ["child workflow", row.childWorkflowId],
    ["message", messageRow ? row.messageId : undefined],
    ["turn", turnRow ? row.turnId : undefined],
    ["offset", row.eventOffset?.toString()]
  ];
  return candidates.flatMap(([label, value]) => (value ? [{ label, value }] : []));
}

/* Minutes have to roll over into hours: a session left open for three hours read as
   "200m 05s", which is arithmetically right and useless to a reader. */
const CLOCK_TIME: Intl.DateTimeFormatOptions = {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit"
};
const timestampFormats = new Map<string, Intl.DateTimeFormat>();

/**
 * A frame timestamp (epoch seconds) as local wall-clock time.
 *
 * One formatter per option set, built once: `toLocaleTimeString` builds a new
 * one on every call, and the chat and the log pane call it once per row.
 */
export function formatTimestamp(
  seconds: number,
  options: Intl.DateTimeFormatOptions = CLOCK_TIME
): string {
  if (!Number.isFinite(seconds)) return "";
  const key = JSON.stringify(options);
  let formatter = timestampFormats.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(undefined, options);
    timestampFormats.set(key, formatter);
  }
  return formatter.format(seconds * 1000);
}

export function formatDuration(seconds: number): string {
  const rounded = Math.max(0, Math.round(seconds));
  if (rounded < 60) return `${rounded}s`;
  const pad = (value: number) => String(value).padStart(2, "0");
  const minutes = Math.floor(rounded / 60);
  if (minutes < 60) return `${minutes}m ${pad(rounded % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${pad(minutes % 60)}m ${pad(rounded % 60)}s`;
}

/**
 * The same reading at the resolution one step is watched at, rather than a run.
 *
 * Two things are this function's own, and only two. Below a second it answers in
 * milliseconds, and between one and ten it keeps a tenth — a tool call that took
 * 2.4s is not a 2s one, and the activity feed is read at that resolution. From ten
 * seconds up there is nothing here formatDuration does not already own.
 *
 * It used to restate the minutes branch instead of delegating, and stopped there,
 * so a three-hour turn read "200m 05s" in the feed for as long as it took anyone
 * to notice — the exact bug formatDuration above was written to fix, reintroduced
 * one component over by copying half of it.
 */
export function formatElapsedDuration(deltaMs: number): string {
  if (deltaMs < 1000) return `${Math.max(1, Math.round(deltaMs))}ms`;

  const seconds = deltaMs / 1000;
  const tenths = Math.round(seconds * 10) / 10;
  if (seconds < 10 && !Number.isInteger(tenths)) return `${tenths.toFixed(1)}s`;
  return formatDuration(seconds);
}
