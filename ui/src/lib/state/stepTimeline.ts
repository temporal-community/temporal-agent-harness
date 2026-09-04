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
  /** 1-based frame position, aligned with AgentRunController.viewIndex. */
  startIndex: number;
  endIndex: number;
  /**
   * 0-based visual lane. Lanes are fixed by kind — model, then tool, then
   * approval — so a row is read structurally rather than by whatever order a
   * greedy packer filled. Same-kind overlap still gets a sub-lane inside its
   * kind's block, so two tools running at once never paint over each other.
   */
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
    if (frame.data.turn_number <= 0) return;
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
      const packed = laneSpans(turn.spans);
      const subagentTurns = turn.subagentTurns
        .filter(
          (subagentTurn) => subagentTurn.turnNumber > 0 && subagentTurn.spans.length > 0
        )
        .map((subagentTurn) => {
          const subagentPacked = laneSpans(subagentTurn.spans);
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

  return { turns: orderedTurns };
}

function spanOrder(a: TimelineSpan, b: TimelineSpan): number {
  return a.startTs - b.startTs || a.startIndex - b.startIndex || a.endTs - b.endTs;
}

/** Top to bottom in every track, so a turn is compared to a turn lane by lane. */
const LANE_ORDER: SpanKind[] = ["model", "tool", "approval"];

/**
 * Named swimlanes, one block per kind, packed greedily *within* the kind.
 *
 * The greedy packer this replaced assigned lanes by overlap alone, so the same
 * model span landed on row 0 in one turn and row 2 in the next, and three
 * concurrent approvals read as three unrelated blobs. A kind owns its rows, and
 * only widens its block when two of its own spans genuinely overlap.
 *
 * A kind with no spans in this track takes no rows at all: an empty approval
 * lane in every turn is height spent saying nothing.
 */
function laneSpans(spans: TimelineSpan[]): { spans: TimelineSpan[]; laneCount: number } {
  const ordered = [...spans].sort(spanOrder);
  const laned: TimelineSpan[] = [];
  let base = 0;

  for (const kind of LANE_ORDER) {
    const laneEnds: number[] = [];
    for (const span of ordered) {
      if (span.kind !== kind) continue;
      let lane = laneEnds.findIndex((endTs) => span.startTs >= endTs);
      if (lane === -1) {
        lane = laneEnds.length;
        laneEnds.push(span.endTs);
      } else {
        laneEnds[lane] = span.endTs;
      }
      laned.push({ ...span, lane: base + lane });
    }
    base += laneEnds.length;
  }

  return { spans: laned, laneCount: Math.max(1, base) };
}

/**
 * The horizontal scale for one turn's row: its own wall-clock, not the run's.
 *
 * Every row used to be drawn against the longest turn, which turned a 45s turn
 * beside an 8m35s one into a sliver of hairlines nobody could read or hit. Rows
 * are no longer comparable by width, which is why each carries its own ruler and
 * its own duration in the label.
 *
 * A subagent that outlives its parent turn's last frame still has to fit, so its
 * end is part of the span the row is scaled to.
 */
export function turnScale(turn: TurnTimeline): number {
  const nested = turn.subagentTurns.reduce(
    (max, subagentTurn) => Math.max(max, subagentTurn.endTs - turn.startTs),
    0
  );
  return Math.max(turn.durationSeconds, nested, 1);
}

/**
 * Where the replay cursor falls inside one turn's row, as 0..1 of its scale, or
 * null when the cursor is not inside this turn at all.
 *
 * The player is linear in event index and the row is linear in wall-clock, so
 * the two are bridged through the (index, timestamp) pairs the spans already
 * carry rather than by pretending they share a domain. Between two known points
 * the reading is interpolated; outside the turn's own frames there is nothing
 * honest to draw.
 */
export function playheadFraction(turn: TurnTimeline, viewIndex: number): number | null {
  const points: Array<[index: number, ts: number]> = [];
  for (const span of [...turn.spans, ...turn.subagentTurns.flatMap((sub) => sub.spans)]) {
    points.push([span.startIndex, span.startTs], [span.endIndex, span.endTs]);
  }
  if (points.length === 0) return null;
  points.sort((a, b) => a[0] - b[0]);

  const first = points[0];
  const last = points[points.length - 1];
  if (viewIndex < first[0] || viewIndex > last[0]) return null;

  let ts = first[1];
  for (let i = 1; i < points.length; i += 1) {
    const [fromIndex, fromTs] = points[i - 1];
    const [toIndex, toTs] = points[i];
    if (viewIndex > toIndex) continue;
    ts =
      toIndex === fromIndex
        ? toTs
        : fromTs + ((viewIndex - fromIndex) / (toIndex - fromIndex)) * (toTs - fromTs);
    break;
  }

  return Math.min(1, Math.max(0, (ts - turn.startTs) / turnScale(turn)));
}

/**
 * Every index at which a span opens or closes, ascending and deduplicated.
 *
 * The step-sized jump the transport was missing. `←`/`→` move one frame, which
 * is finer than anything a reader is looking for, and `Shift`+those move a whole
 * turn, which in a run of any length is most of it. A span edge is the middle
 * granularity, and it is not a new one: a span *is* what the state-flow graph
 * draws as a card — one model interaction, one tool call, one approval wait — so
 * walking this list steps between the things already on the canvas.
 *
 * Subagent spans are in, for the same reason they are in the waterfall: a
 * delegated tool call is a step of the run whoever is reading it cares about.
 */
export function buildStepBoundaries(timeline: StepTimeline): number[] {
  const indices = new Set<number>();
  for (const turn of timeline.turns) {
    const spans = [...turn.spans, ...turn.subagentTurns.flatMap((sub) => sub.spans)];
    for (const span of spans) {
      indices.add(span.startIndex);
      indices.add(span.endIndex);
    }
  }
  return [...indices].sort((a, b) => a - b);
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
