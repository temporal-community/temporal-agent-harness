import type {
  AgentEventType,
  AgentSseFrame,
  FileCitationAnnotation,
  JsonRecord,
  ToolId
} from "$lib/api/types";
import {
  formatCost,
  formatTokens,
  summarizeCost,
  type UsageTotals
} from "$lib/cost/pricing";

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
  offset: number;
  turnNumber: number;
  sourceTurnNumber: number;
  parentTurnNumber?: number;
  workflowId?: string;
  sourceLabel?: string;
  turnId: string;
  timestamp: number;
  event: AgentEventType;
  actor: ReplayActor;
  tone: ReplayTone;
  label: string;
  status?: string;
  body?: string;
  detail?: string;
  model?: string | null;
  toolId?: ToolId;
  toolName?: string;
  input?: JsonRecord;
  output?: string;
  citations: FileCitationAnnotation[];
  usage?: UsageTotals;
  estimatedCostUsd?: number | null;
  marker?: ReplayMarkerTone;
  markerLabel?: string;
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

function thoughtText(delta: JsonRecord): string {
  const content = delta.content;
  if (typeof content === "object" && content != null && "text" in content) {
    return String((content as { text?: unknown }).text ?? "");
  }
  return "";
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

function modelUsageBody(usage: UsageTotals, cost: number | null): string {
  return `${formatTokens(usage.total)} tokens, ${formatCost(cost)}`;
}

function normalizeReplayLogFrame(item: AgentSseFrame | ReplayLogFrame): ReplayLogFrame {
  return "frame" in item ? item : { frame: item, role: "parent" };
}

function rowFromFrame(
  entry: ReplayLogFrame,
  frameIndex: number
): ReplayLogRow | null {
  const { frame } = entry;
  if (!("type" in frame.data)) {
    return {
      id: `${entry.workflowId ?? "parent"}-stream-error-${frame.data.offset}`,
      index: frameIndex + 1,
      offset: frame.data.offset,
      turnNumber: 0,
      sourceTurnNumber: 0,
      workflowId: entry.workflowId,
      sourceLabel: entry.label,
      turnId: "client",
      timestamp: 0,
      event: "error",
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
    id: `${entry.workflowId ?? "parent"}-${frame.data.type}-${frame.data.offset}`,
    index: frameIndex + 1,
    offset: frame.data.offset,
    turnNumber,
    sourceTurnNumber,
    parentTurnNumber: entry.role === "subagent" ? entry.parentTurnNumber : undefined,
    workflowId: entry.workflowId,
    sourceLabel: entry.label,
    turnId: frame.data.turn_id,
    timestamp: frame.data.timestamp,
    event: frame.data.type,
    citations: [] as FileCitationAnnotation[]
  };

  if (frame.event === "turn_started") {
    return {
      ...base,
      actor: "user",
      tone: "queue",
      label: "User message received",
      body: renderUserMessage(frame.data.user_message)
    };
  }

  if (frame.event === "message_queued") {
    return {
      ...base,
      actor: "queue",
      tone: "queue",
      label: "Message queued",
      body: renderUserMessage(frame.data.user_message),
      marker: "queue",
      markerLabel: "queued turn"
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
      body: modelUsageBody(summary.tokens, summary.estimatedCostUsd),
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

  if (frame.event === "subagent_started") {
    return {
      ...base,
      actor: "subagent",
      tone: "agent",
      label: "Subagent started",
      body: `${frame.data.agent_key} · ${frame.data.handle}`,
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
      body: `${frame.data.function} → turn ${frame.data.subagent_turn}`,
      detail: `${frame.data.agent_key} · ${frame.data.handle}`,
      status: "dispatched"
    };
  }

  if (frame.event === "subagent_stopped") {
    return {
      ...base,
      actor: "subagent",
      tone: "done",
      label: "Subagent stopped",
      body: `${frame.data.agent_key} · ${frame.data.handle}`,
      detail: frame.data.workflow_id,
      status: "stopped"
    };
  }

  if (frame.event === "thought_summary") {
    return {
      ...base,
      actor: "reasoning",
      tone: "model",
      label: "Reasoning summary",
      body: thoughtText(frame.data.delta)
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

  if (frame.event === "reply") {
    return {
      ...base,
      actor: "agent",
      tone: "done",
      label: "Final reply",
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

  if (frame.event === "error") {
    return {
      ...base,
      actor: "error",
      tone: "error",
      label: "Agent error",
      body: frame.data.message,
      marker: "error",
      markerLabel: "agent error"
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
  const rows = input
    .map((item, index) => rowFromFrame(normalizeReplayLogFrame(item), index))
    .filter((row): row is ReplayLogRow => row != null);

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

export function buildReplayMarkers(input: Array<AgentSseFrame | ReplayLogFrame>): ReplayMarker[] {
  return buildReplayLog(input).rows
    .filter((row) => row.marker)
    .map((row) => ({
      id: `marker-${row.offset}`,
      index: row.index,
      turnNumber: row.turnNumber,
      tone: row.marker ?? "queue",
      label: row.markerLabel ?? row.label
    }));
}

export function formatDuration(seconds: number): string {
  const rounded = Math.max(0, Math.round(seconds));
  if (rounded < 60) return `${rounded}s`;
  return `${Math.floor(rounded / 60)}m ${String(rounded % 60).padStart(2, "0")}s`;
}
