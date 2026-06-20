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

export type TimelineRole = "parent" | "subagent";

export interface StepTimelineFrame {
  frame: AgentSseFrame;
  workflowId?: string;
  role?: TimelineRole;
  label?: string;
  parentTurnNumber?: number;
}

export interface TimelineTurnBase {
  turnNumber: number;
  startTs: number;
  endTs: number;
  durationSeconds: number;
  preview: string;
  spans: TimelineSpan[];
  laneCount: number;
}

export interface SubagentTurnTimeline extends TimelineTurnBase {
  role: "subagent";
  workflowId: string;
  label: string;
  parentTurnNumber: number;
}

export interface TurnTimeline extends TimelineTurnBase {
  role: "parent";
  subagentTurns: SubagentTurnTimeline[];
}

export interface StepTimeline {
  turns: TurnTimeline[];
  /** Longest turn wall-clock, used as the shared horizontal scale. */
  maxTurnDuration: number;
}

interface OpenSpan {
  scope: TimelineScope;
  turnNumber: number;
  kind: SpanKind;
  label: string;
  detail?: string;
  startTs: number;
  startIndex: number;
}

interface TimelineScope {
  key: string;
  role: TimelineRole;
  workflowId: string;
  label: string;
  turnNumber: number;
  parentTurnNumber: number;
}

interface LastSeenFrame {
  timestamp: number;
  index: number;
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
export function buildStepTimeline(input: Array<AgentSseFrame | StepTimelineFrame>): StepTimeline {
  const turns = new Map<number, TurnTimeline>();
  const openModel = new Map<string, OpenSpan>();
  const openTool = new Map<string, OpenSpan>();
  const openApproval = new Map<string, OpenSpan>();
  const lastSeenByScope = new Map<string, LastSeenFrame>();
  const previewByScope = new Map<string, string>();

  function frameFor(item: AgentSseFrame | StepTimelineFrame): StepTimelineFrame {
    return "frame" in item ? item : { frame: item, role: "parent" };
  }

  function parentTurnFor(turnNumber: number, timestamp: number): TurnTimeline {
    let turn = turns.get(turnNumber);
    if (!turn) {
      turn = {
        role: "parent",
        turnNumber,
        startTs: timestamp,
        endTs: timestamp,
        durationSeconds: 0,
        preview: "",
        spans: [],
        laneCount: 1,
        subagentTurns: []
      };
      turns.set(turnNumber, turn);
    }
    turn.startTs = Math.min(turn.startTs, timestamp);
    turn.endTs = Math.max(turn.endTs, timestamp);
    return turn;
  }

  function subagentTurnFor(scope: TimelineScope, timestamp: number): SubagentTurnTimeline {
    const parent = parentTurnFor(scope.parentTurnNumber, timestamp);
    let turn = parent.subagentTurns.find(
      (item) =>
        item.workflowId === scope.workflowId &&
        item.turnNumber === scope.turnNumber &&
        item.parentTurnNumber === scope.parentTurnNumber
    );
    if (!turn) {
      turn = {
        role: "subagent",
        workflowId: scope.workflowId,
        label: scope.label,
        parentTurnNumber: scope.parentTurnNumber,
        turnNumber: scope.turnNumber,
        startTs: timestamp,
        endTs: timestamp,
        durationSeconds: 0,
        preview: previewByScope.get(scope.key) ?? "",
        spans: [],
        laneCount: 1
      };
      parent.subagentTurns.push(turn);
    }
    turn.startTs = Math.min(turn.startTs, timestamp);
    turn.endTs = Math.max(turn.endTs, timestamp);
    return turn;
  }

  function turnFor(scope: TimelineScope, timestamp: number): TimelineTurnBase {
    if (scope.role === "subagent") return subagentTurnFor(scope, timestamp);
    return parentTurnFor(scope.turnNumber, timestamp);
  }

  function scopeFor(entry: StepTimelineFrame, turnNumber: number): TimelineScope {
    const role = entry.role ?? "parent";
    const workflowId = entry.workflowId ?? role;
    if (role === "subagent") {
      const parentTurnNumber = entry.parentTurnNumber ?? turnNumber;
      return {
        key: `subagent:${workflowId}:${parentTurnNumber}:${turnNumber}`,
        role,
        workflowId,
        label: entry.label ?? "Subagent",
        turnNumber,
        parentTurnNumber
      };
    }
    return {
      key: parentScopeKey(turnNumber),
      role,
      workflowId,
      label: entry.label ?? "Parent agent",
      turnNumber,
      parentTurnNumber: turnNumber
    };
  }

  function modelKey(scope: TimelineScope): string {
    return `${scope.key}:model`;
  }

  function parentScopeKey(turnNumber: number): string {
    return `parent:${turnNumber}`;
  }

  function keyedTool(scope: TimelineScope, toolId: string): string {
    return `${scope.key}:${toolId}`;
  }

  function closeSpan(
    open: OpenSpan,
    endTs: number,
    endIndex: number,
    tone: SpanTone,
    detail?: string
  ): void {
    turnFor(open.scope, open.startTs);
    const turn = turnFor(open.scope, endTs);
    turn.spans.push({
      id: `${open.kind}-${open.startIndex}-${endIndex}`,
      turnNumber: open.turnNumber,
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
    closeSpan(open, endTs, endIndex, tone, detail);
    map.delete(key);
  }

  function closeOpenSpansForTurn(
    scope: TimelineScope,
    endTs: number,
    endIndex: number,
    tone: SpanTone,
    detail?: string
  ): void {
    closeOpenSpan(openModel, modelKey(scope), endTs, endIndex, tone, detail);
    for (const [toolId, open] of openTool) {
      if (open.scope.key === scope.key) {
        closeOpenSpan(openTool, toolId, endTs, endIndex, tone, detail);
      }
    }
    for (const [toolId, open] of openApproval) {
      if (open.scope.key === scope.key) {
        closeOpenSpan(openApproval, toolId, endTs, endIndex, tone, detail);
      }
    }
  }

  input.forEach((item, position) => {
    const entry = frameFor(item);
    const { frame } = entry;
    if (!("type" in frame.data)) return;
    const index = position + 1;
    const { turn_number: turnNumber, timestamp } = frame.data;
    const scope = scopeFor(entry, turnNumber);
    if (scope.role === "parent") turnFor(scope, timestamp);
    lastSeenByScope.set(scope.key, { timestamp, index });
    if (scope.role === "subagent") {
      lastSeenByScope.set(parentScopeKey(scope.parentTurnNumber), { timestamp, index });
    }

    switch (frame.event) {
      case "turn_started":
        previewByScope.set(scope.key, frame.data.user_message);
        if (scope.role === "parent") turnFor(scope, timestamp).preview = frame.data.user_message;
        break;
      case "model_interaction_started":
        closeOpenSpan(
          openModel,
          modelKey(scope),
          timestamp,
          index,
          "error",
          "Model span restarted before completion."
        );
        openModel.set(modelKey(scope), {
          scope,
          turnNumber,
          kind: "model",
          label: spanLabel("model", frame.data.model ?? "model"),
          startTs: timestamp,
          startIndex: index
        });
        break;
      case "model_interaction_ended": {
        closeOpenSpan(openModel, modelKey(scope), timestamp, index, "done");
        break;
      }
      case "tool_start": {
        const key = keyedTool(scope, frame.data.tool_id);
        if (!openTool.has(key)) {
          openTool.set(key, {
            scope,
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
        closeOpenSpan(openTool, keyedTool(scope, frame.data.tool_id), timestamp, index, "done");
        break;
      }
      case "tool_error": {
        closeOpenSpan(
          openTool,
          keyedTool(scope, frame.data.tool_id),
          timestamp,
          index,
          "error",
          frame.data.message
        );
        break;
      }
      case "tool_approval_requested":
        openApproval.set(keyedTool(scope, frame.data.tool_id), {
          scope,
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
          keyedTool(scope, frame.data.tool_id),
          timestamp,
          index,
          frame.data.approved ? "done" : "error",
          frame.data.reason ?? undefined
        );
        break;
      }
      case "error": {
        closeOpenSpansForTurn(scope, timestamp, index, "error", frame.data.message);
        break;
      }
    }
  });

  // Flush spans that never resolved within the supplied frames.
  const flush = (map: ReadonlyMap<unknown, OpenSpan>, tone: SpanTone) => {
    for (const [, open] of map) {
      const lastSeen = lastSeenByScope.get(open.scope.key);
      closeSpan(
        open,
        Math.max(lastSeen?.timestamp ?? open.startTs, open.startTs),
        lastSeen?.index ?? input.length,
        tone
      );
      const turn = turnFor(open.scope, open.startTs);
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
      const subagentTurns = turn.subagentTurns
        .filter(
          (subagentTurn) => subagentTurn.turnNumber > 0 && subagentTurn.spans.length > 0
        )
        .map((subagentTurn) => {
          const subagentPacked = packSpans(subagentTurn.spans);
          return {
            ...subagentTurn,
            durationSeconds: Math.max(0, subagentTurn.endTs - subagentTurn.startTs),
            spans: subagentPacked.spans,
            laneCount: subagentPacked.laneCount
          };
        })
        .sort((a, b) => a.startTs - b.startTs || a.label.localeCompare(b.label));
      return {
        ...turn,
        durationSeconds: Math.max(0, turn.endTs - turn.startTs),
        spans: packed.spans,
        laneCount: packed.laneCount,
        subagentTurns
      };
    })
    .sort((a, b) => a.turnNumber - b.turnNumber);

  const maxTurnDuration = orderedTurns.reduce((max, turn) => {
    const nestedMax = turn.subagentTurns.reduce(
      (nested, subagentTurn) => Math.max(nested, subagentTurn.endTs - turn.startTs),
      0
    );
    return Math.max(max, turn.durationSeconds, nestedMax);
  }, 1);

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
    addTurnAggregate(totals, turn.spans);
    for (const subagentTurn of turn.subagentTurns) {
      addTurnAggregate(totals, subagentTurn.spans);
    }
  }
  const order: SpanKind[] = ["model", "tool", "approval"];
  return order
    .map((kind) => totals.get(kind))
    .filter((agg): agg is SpanAggregate => agg != null);
}

function addTurnAggregate(totals: Map<SpanKind, SpanAggregate>, spans: TimelineSpan[]): void {
  for (const span of spans) {
    const agg = totals.get(span.kind) ?? {
      kind: span.kind,
      count: 0,
      totalSeconds: 0
    };
    agg.count += 1;
    totals.set(span.kind, agg);
  }

  for (const { kind, seconds } of exclusiveTurnSegments(spans)) {
    const agg = totals.get(kind) ?? {
      kind,
      count: 0,
      totalSeconds: 0
    };
    agg.totalSeconds += seconds;
    totals.set(kind, agg);
  }
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
