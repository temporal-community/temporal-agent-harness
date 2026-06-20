import type { AgentSseFrame } from "$lib/api/types";
import { formatTokens, type UsageTotals } from "$lib/cost/pricing";

export type SpanKind = "model" | "tool" | "approval";
export type SpanTone = "model" | "tool" | "approval" | "error" | "done";

export interface TimelineSpan {
  id: string;
  turnNumber: number;
  kind: SpanKind;
  label: string;
  detail?: string;
  tone: SpanTone;
  startTs: number;
  endTs: number;
  durationSeconds: number;
  /** 1-based frame position, aligned with MockRunController.viewIndex. */
  startIndex: number;
  endIndex: number;
  /** 0-based visual lane, assigned so overlapping spans do not obscure each other. */
  lane: number;
  /** Started but not resolved within the supplied frames. */
  ongoing: boolean;
}

export interface TurnTimeline {
  turnNumber: number;
  startTs: number;
  endTs: number;
  durationSeconds: number;
  preview: string;
  spans: TimelineSpan[];
  laneCount: number;
}

export interface StepTimeline {
  turns: TurnTimeline[];
  /** Longest turn wall-clock, used as the shared horizontal scale. */
  maxTurnDuration: number;
}

interface OpenSpan {
  turnNumber: number;
  kind: SpanKind;
  label: string;
  detail?: string;
  startTs: number;
  startIndex: number;
}

function spanLabel(kind: SpanKind, name: string): string {
  if (kind === "model") return name;
  if (kind === "approval") return `approval · ${name}`;
  return name;
}

/**
 * Pairs start/end frames into duration spans so the UI can show *where time
 * goes* inside each turn (model latency, tool execution, approval waits).
 */
export function buildStepTimeline(frames: AgentSseFrame[]): StepTimeline {
  const turns = new Map<number, TurnTimeline>();
  const openModel = new Map<number, OpenSpan>();
  const openTool = new Map<string, OpenSpan>();
  const openApproval = new Map<string, OpenSpan>();

  function turnFor(turnNumber: number, timestamp: number): TurnTimeline {
    let turn = turns.get(turnNumber);
    if (!turn) {
      turn = {
        turnNumber,
        startTs: timestamp,
        endTs: timestamp,
        durationSeconds: 0,
        preview: "",
        spans: [],
        laneCount: 1
      };
      turns.set(turnNumber, turn);
    }
    turn.startTs = Math.min(turn.startTs, timestamp);
    turn.endTs = Math.max(turn.endTs, timestamp);
    return turn;
  }

  function closeSpan(
    turnNumber: number,
    open: OpenSpan,
    endTs: number,
    endIndex: number,
    tone: SpanTone,
    detail?: string
  ): void {
    const turn = turnFor(turnNumber, endTs);
    turn.spans.push({
      id: `${open.kind}-${open.startIndex}-${endIndex}`,
      turnNumber,
      kind: open.kind,
      label: open.label,
      detail: detail ?? open.detail,
      tone,
      startTs: open.startTs,
      endTs,
      durationSeconds: Math.max(0, endTs - open.startTs),
      startIndex: open.startIndex,
      endIndex,
      lane: 0,
      ongoing: false
    });
  }

  function closeOpenSpan<TKey>(
    map: Map<TKey, OpenSpan>,
    key: TKey,
    endTs: number,
    endIndex: number,
    tone: SpanTone,
    detail?: string
  ): void {
    const open = map.get(key);
    if (!open) return;
    closeSpan(open.turnNumber, open, endTs, endIndex, tone, detail);
    map.delete(key);
  }

  function closeOpenSpansForTurn(
    turnNumber: number,
    endTs: number,
    endIndex: number,
    tone: SpanTone,
    detail?: string
  ): void {
    closeOpenSpan(openModel, turnNumber, endTs, endIndex, tone, detail);
    for (const [toolId, open] of openTool) {
      if (open.turnNumber === turnNumber) {
        closeOpenSpan(openTool, toolId, endTs, endIndex, tone, detail);
      }
    }
    for (const [toolId, open] of openApproval) {
      if (open.turnNumber === turnNumber) {
        closeOpenSpan(openApproval, toolId, endTs, endIndex, tone, detail);
      }
    }
  }

  frames.forEach((frame, position) => {
    if (!("type" in frame.data)) return;
    const index = position + 1;
    const { turn_number: turnNumber, timestamp } = frame.data;
    turnFor(turnNumber, timestamp);

    switch (frame.event) {
      case "turn_started":
        turns.get(turnNumber)!.preview = frame.data.user_message;
        break;
      case "model_interaction_started":
        closeOpenSpan(
          openModel,
          turnNumber,
          timestamp,
          index,
          "error",
          "Model span restarted before completion."
        );
        openModel.set(turnNumber, {
          turnNumber,
          kind: "model",
          label: spanLabel("model", frame.data.model ?? "model"),
          startTs: timestamp,
          startIndex: index
        });
        break;
      case "model_interaction_ended": {
        closeOpenSpan(openModel, turnNumber, timestamp, index, "done");
        break;
      }
      case "tool_start": {
        if (!openTool.has(frame.data.tool_id)) {
          openTool.set(frame.data.tool_id, {
            turnNumber,
            kind: "tool",
            label: spanLabel("tool", frame.data.tool_name),
            startTs: timestamp,
            startIndex: index
          });
        }
        break;
      }
      case "tool_end": {
        closeOpenSpan(openTool, frame.data.tool_id, timestamp, index, "done");
        break;
      }
      case "tool_error": {
        closeOpenSpan(openTool, frame.data.tool_id, timestamp, index, "error", frame.data.message);
        break;
      }
      case "tool_approval_requested":
        openApproval.set(frame.data.tool_id, {
          turnNumber,
          kind: "approval",
          label: spanLabel("approval", frame.data.tool_name),
          startTs: timestamp,
          startIndex: index
        });
        break;
      case "tool_approval_resolved": {
        closeOpenSpan(
          openApproval,
          frame.data.tool_id,
          timestamp,
          index,
          frame.data.approved ? "done" : "error",
          frame.data.reason ?? undefined
        );
        break;
      }
      case "error": {
        closeOpenSpansForTurn(turnNumber, timestamp, index, "error", frame.data.message);
        break;
      }
    }
  });

  // Flush spans that never resolved within the supplied frames.
  const lastIndex = frames.length;
  const lastTs = frames.at(-1)?.data && "type" in frames.at(-1)!.data
    ? (frames.at(-1)!.data as { timestamp: number }).timestamp
    : 0;
  const flush = (map: ReadonlyMap<unknown, OpenSpan>, tone: SpanTone) => {
    for (const [, open] of map) {
      closeSpan(open.turnNumber, open, Math.max(lastTs, open.startTs), lastIndex, tone);
      const turn = turns.get(open.turnNumber);
      const span = turn?.spans.at(-1);
      if (span) span.ongoing = true;
    }
  };
  flush(openModel, "model");
  flush(openTool, "tool");
  flush(openApproval, "approval");

  const orderedTurns = [...turns.values()]
    .filter((turn) => turn.turnNumber > 0)
    .map((turn) => {
      const packed = packSpans(turn.spans);
      return {
        ...turn,
        durationSeconds: Math.max(0, turn.endTs - turn.startTs),
        spans: packed.spans,
        laneCount: packed.laneCount
      };
    })
    .sort((a, b) => a.turnNumber - b.turnNumber);

  const maxTurnDuration = orderedTurns.reduce(
    (max, turn) => Math.max(max, turn.durationSeconds),
    1
  );

  return { turns: orderedTurns, maxTurnDuration };
}

function spanOrder(a: TimelineSpan, b: TimelineSpan): number {
  return a.startTs - b.startTs || a.startIndex - b.startIndex || a.endTs - b.endTs;
}

function packSpans(spans: TimelineSpan[]): { spans: TimelineSpan[]; laneCount: number } {
  const laneEnds: number[] = [];
  const packed = [...spans].sort(spanOrder).map((span) => {
    let lane = laneEnds.findIndex((endTs) => span.startTs >= endTs);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(span.endTs);
    } else {
      laneEnds[lane] = span.endTs;
    }
    return { ...span, lane };
  });

  return { spans: packed, laneCount: Math.max(1, laneEnds.length) };
}

export interface SpanAggregate {
  kind: SpanKind;
  count: number;
  totalSeconds: number;
}

/** Roll spans up by kind for the run-summary "where time goes" read-out. */
export function aggregateSpans(timeline: StepTimeline): SpanAggregate[] {
  const totals = new Map<SpanKind, SpanAggregate>();
  for (const turn of timeline.turns) {
    for (const span of turn.spans) {
      const agg = totals.get(span.kind) ?? {
        kind: span.kind,
        count: 0,
        totalSeconds: 0
      };
      agg.count += 1;
      totals.set(span.kind, agg);
    }

    for (const { kind, seconds } of exclusiveTurnSegments(turn.spans)) {
      const agg = totals.get(kind) ?? {
        kind,
        count: 0,
        totalSeconds: 0
      };
      agg.totalSeconds += seconds;
      totals.set(kind, agg);
    }
  }
  const order: SpanKind[] = ["model", "tool", "approval"];
  return order
    .map((kind) => totals.get(kind))
    .filter((agg): agg is SpanAggregate => agg != null);
}

function exclusiveTurnSegments(spans: TimelineSpan[]): { kind: SpanKind; seconds: number }[] {
  const boundaries = [...new Set(spans.flatMap((span) => [span.startTs, span.endTs]))]
    .sort((a, b) => a - b);
  const segments: { kind: SpanKind; seconds: number }[] = [];

  for (let index = 0; index < boundaries.length - 1; index += 1) {
    const startTs = boundaries[index];
    const endTs = boundaries[index + 1];
    if (endTs <= startTs) continue;

    const covering = spans
      .filter((span) => span.startTs < endTs && span.endTs > startTs)
      .sort(spanPriority);
    const topSpan = covering[0];
    if (!topSpan) continue;
    segments.push({ kind: topSpan.kind, seconds: endTs - startTs });
  }

  return segments;
}

function spanPriority(a: TimelineSpan, b: TimelineSpan): number {
  const order: Record<SpanKind, number> = {
    approval: 0,
    tool: 1,
    model: 2
  };
  return order[a.kind] - order[b.kind] || spanOrder(a, b);
}

export function tokensLabel(tokens: UsageTotals): string {
  return `${formatTokens(tokens.total)} tok`;
}
