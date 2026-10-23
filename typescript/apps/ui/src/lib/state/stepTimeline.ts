import type { AgentSseFrame } from "$lib/api/types";
import { renderUserMessage } from "$lib/state/inboundMessageText";
import { rootLogOffset } from "$lib/state/historyGap";
import type {
  CollapsedGap,
  WorkflowTimelineBounds
} from "$lib/state/workflowTimelineScale";

/* "approval" is the whole gate — requested until resolved. "evaluation" is the automatic
 * approval check nested inside it. They are separate kinds on purpose: the gate bar
 * otherwise fuses model latency and human latency into one number, and "is our approval
 * p99 the approver or the people?" is the question this view exists to answer. */
export type SpanKind = "model" | "tool" | "approval" | "evaluation";
export type SpanTone = "model" | "tool" | "approval" | "error" | "done";

export interface TimelineSpan {
  id: string;
  turnNumber: number;
  kind: SpanKind;
  waitType?: "approval" | "evaluation" | "callback";
  /** The `tool_id` a tool call, its approval, checks and callback all carry. */
  toolId?: string;
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
  /**
   * This turn's frames straddle a seam in the run's history — some of it was
   * already trimmed out of the stream when this view resumed (see historyGap.ts).
   *
   * Parent-only, because the seam is a discontinuity in the ROOT log's offsets
   * and a parent turn is the coarsest thing that can own one. It says where the
   * hole is and nothing about its size, so a turn carrying it must not be read as
   * having lost any particular step — only as not being able to account for the
   * steps it does not show.
   */
  historyGap: boolean;
}

export interface StepTimeline {
  turns: TurnTimeline[];
  workflow: WorkflowTimeline;
}

export interface WorkflowTimelineRow {
  key: string;
  source: "span" | "subagent";
  kind: SpanKind | "subagent";
  label: string;
  detail?: string;
  tone: SpanTone;
  startTs: number;
  endTs: number;
  startIndex: number;
  endIndex: number;
  ongoing: boolean;
  parentTurnNumber: number;
  workflowId: string;
  subagentTurnNumber?: number;
  /** Tree depth: rows are in preorder, so a row's descendants are the rows after it that
      are deeper than it. */
  depth: number;
  parentKey?: string;
  waitType?: TimelineSpan["waitType"];
  toolId?: string;
}

export interface WorkflowTimeline {
  subagents: WorkflowSubagentLifecycle[];
  points: readonly WorkflowTimelinePoint[];
  bounds: WorkflowTimelineBounds;
  gaps: readonly CollapsedGap[];
  /** Whether any span or subagent is still open at the newest frame. */
  ongoing: boolean;
  rowCount: number;
  rowAt(index: number): WorkflowTimelineRow | undefined;
  /** Every row in order: the same rows as `rowAt`, without a lookup per row. */
  rows?(): readonly WorkflowTimelineRow[];
}

export type WorkflowTimelinePoint = readonly [index: number, timestamp: number];

export interface WorkflowSubagentLifecycle extends WorkflowTimelineRow {
  key: string;
  source: "subagent";
  kind: "subagent";
  subagentId: string;
  /** The subagent that started this one; absent when the root agent did. */
  parentWorkflowId?: string;
}

interface OpenSpan {
  scope: TimelineScope;
  turnNumber: number;
  kind: SpanKind;
  toolId?: string;
  label: string;
  detail?: string;
  waitType?: TimelineSpan["waitType"];
  startTs: number;
  startIndex: number;
}

interface OpenSubagent {
  lifecycle: WorkflowSubagentLifecycle;
  scopeKey: string;
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
  if (kind === "evaluation") return `approval check · ${name}`;
  return name;
}

type StepTimelineInput = AgentSseFrame | StepTimelineFrame;

/**
 * Pairs start/end frames into duration spans so the UI can show *where time
 * goes* inside each turn (model latency, tool execution, approval waits).
 */
export function buildStepTimeline(input: readonly StepTimelineInput[]): StepTimeline {
  return stepTimelineBuilder()(input);
}

/**
 * The step timeline, folded forward and kept between calls, the way replayLogBuilder
 * keeps the replay log.
 *
 * Rebuilding it per commit was a pass over every frame plus a lane pack of every turn,
 * which at 50,000 spans cost more than the commit's budget on its own. While the last
 * frame folded is still at its index, only the new frames are folded in, and only the
 * turns they touched — plus any turn holding a still-open span, whose drawn end follows
 * the newest frame in its scope — are packed again. A shorter input, or one that is not
 * an extension, starts over. Every other turn keeps its identity, which is what lets a
 * row nothing touched skip its redraw.
 */
export function stepTimelineBuilder(): (input: readonly StepTimelineInput[]) => StepTimeline {
  let turns = new Map<number, TurnTimeline>();
  let openModel = new Map<string, OpenSpan>();
  let openTool = new Map<string, OpenSpan>();
  let openApproval = new Map<string, OpenSpan>();
  let openCallback = new Map<string, OpenSpan>();
  /* Keyed by evaluation_id, NOT tool_id: an agent may chain several evaluators over one
     gated call, and pairing those by tool_id would fuse them into one span. */
  let openEvaluation = new Map<string, OpenSpan>();
  let openSubagent = new Map<string, OpenSubagent>();
  let closedSubagents: WorkflowSubagentLifecycle[] = [];
  let lastSeenByScope = new Map<string, LastSeenFrame>();
  let previewByScope = new Map<string, string>();
  let seen = 0;
  let last: StepTimelineInput | undefined;
  let highestOffset: number | null = null;
  /* Held rather than applied where it is found, because the frame just past a seam
     may be one this projection skips — a `turn: 0` session frame or a stream error
     — and dropping the seam with it would leave the gap unreported. It lands on the
     first turn that can own it instead. */
  let pendingGap = false;
  /** Each parent turn as last returned, laned and with its open spans flushed. */
  let built = new Map<number, TurnTimeline>();
  /** Parent turns written to since the last return. */
  const dirty = new Set<number>();
  const runPoints: WorkflowTimelinePoint[] = [];
  const workflowRows = new WorkflowRowBlocks();
  const idleGaps = new WorkflowIdleGapTracker();

  function frameFor(item: AgentSseFrame | StepTimelineFrame): StepTimelineFrame {
    return "frame" in item ? item : { frame: item, role: "parent" };
  }

  function parentTurnFor(turnNumber: number, timestamp: number): TurnTimeline {
    dirty.add(turnNumber);
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
        subagentTurns: [],
        historyGap: false
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
      id: `${open.kind}-${open.startIndex}`,
      turnNumber: open.turnNumber,
      kind: open.kind,
      waitType: open.waitType,
      toolId: open.toolId,
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
    idleGaps.close(endTs);
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
    for (const [evaluationId, open] of openEvaluation) {
      if (open.scope.key === scope.key) {
        closeOpenSpan(openEvaluation, evaluationId, endTs, endIndex, tone, detail);
      }
    }
    for (const [toolId, open] of openCallback) {
      if (open.scope.key === scope.key) {
        closeOpenSpan(openCallback, toolId, endTs, endIndex, tone, detail);
      }
    }
  }

  /* findHistoryGaps' high-water mark, carried between calls. */
  function foldFrame(item: StepTimelineInput, position: number): void {
    const offset = rootLogOffset(item);
    if (offset != null) {
      if (highestOffset != null && offset > highestOffset + 1) pendingGap = true;
      highestOffset = highestOffset == null ? offset : Math.max(highestOffset, offset);
    }
    const entry = frameFor(item);
    const { frame } = entry;
    if (!("type" in frame.data)) return;
    if (frame.data.turn_number <= 0) return;
    const index = position + 1;
    const { turn_number: turnNumber, timestamp } = frame.data;
    idleGaps.observe(timestamp);
    runPoints.push([index, timestamp]);
    const scope = scopeFor(entry, turnNumber);
    if (scope.role === "parent") turnFor(scope, timestamp);
    if (pendingGap) {
      parentTurnFor(scope.parentTurnNumber, timestamp).historyGap = true;
      pendingGap = false;
    }
    lastSeenByScope.set(scope.key, { timestamp, index });
    if (scope.role === "subagent") {
      lastSeenByScope.set(parentScopeKey(scope.parentTurnNumber), { timestamp, index });
    }

    switch (frame.event) {
      case "message_accepted": {
        // The turn's preview comes from admission, not from turn_started — a joining message
        // has no turn_started of its own, and a shared turn's preview is simply the last
        // message admitted into it.
        const preview = renderUserMessage(frame.data.handler, frame.data.payload);
        previewByScope.set(scope.key, preview);
        if (scope.role === "parent") turnFor(scope, timestamp).preview = preview;
        break;
      }
      case "model_interaction_started":
        closeOpenSpan(
          openModel,
          modelKey(scope),
          timestamp,
          index,
          "error",
          "Model span restarted before completion."
        );
        idleGaps.open(timestamp);
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
          idleGaps.open(timestamp);
          openTool.set(key, {
            scope,
            turnNumber,
            kind: "tool",
            toolId: frame.data.tool_id,
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
        if (!openApproval.has(keyedTool(scope, frame.data.tool_id))) {
          idleGaps.open(timestamp);
        }
        openApproval.set(keyedTool(scope, frame.data.tool_id), {
          scope,
          turnNumber,
          kind: "approval",
          waitType: "approval",
          toolId: frame.data.tool_id,
          label: spanLabel("approval", frame.data.tool_name),
          startTs: timestamp,
          startIndex: index
        });
        break;
      case "auto_approval_evaluation_started":
        if (!openEvaluation.has(keyedTool(scope, frame.data.evaluation_id))) {
          idleGaps.open(timestamp);
        }
        openEvaluation.set(keyedTool(scope, frame.data.evaluation_id), {
          scope,
          turnNumber,
          kind: "evaluation",
          waitType: "evaluation",
          toolId: frame.data.tool_id,
          label: spanLabel("evaluation", frame.data.evaluator),
          startTs: timestamp,
          startIndex: index
        });
        break;
      case "auto_approval_evaluation_ended": {
        /* An escalate did not fail — it correctly declined to decide — so it stays
           "approval" toned, the same as the still-open gate it hands back to. */
        const verdict = frame.data.verdict;
        closeOpenSpan(
          openEvaluation,
          keyedTool(scope, frame.data.evaluation_id),
          timestamp,
          index,
          verdict === "approve" ? "done" : verdict === "deny" ? "error" : "approval",
          frame.data.reason ?? undefined
        );
        break;
      }
      case "auto_approval_evaluation_superseded": {
        /* Cancelled, not failed — the gate was answered elsewhere. Neutral-toned so a
           waterfall full of them does not read as a run full of errors. */
        closeOpenSpan(
          openEvaluation,
          keyedTool(scope, frame.data.evaluation_id),
          timestamp,
          index,
          "approval",
          "cancelled — the gate was decided first"
        );
        break;
      }
      case "auto_approval_evaluation_error": {
        closeOpenSpan(
          openEvaluation,
          keyedTool(scope, frame.data.evaluation_id),
          timestamp,
          index,
          "error",
          frame.data.message
        );
        break;
      }
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
      case "callback_requested":
        if (!openCallback.has(keyedTool(scope, frame.data.tool_id))) {
          idleGaps.open(timestamp);
        }
        openCallback.set(keyedTool(scope, frame.data.tool_id), {
          scope,
          turnNumber,
          kind: "approval",
          waitType: "callback",
          toolId: frame.data.tool_id,
          label: `callback · ${frame.data.tool_name}`,
          startTs: timestamp,
          startIndex: index
        });
        break;
      case "callback_resolved": {
        closeOpenSpan(
          openCallback,
          keyedTool(scope, frame.data.tool_id),
          timestamp,
          index,
          frame.data.outcome === "ok" ? "done" : "error",
          frame.data.error ?? undefined
        );
        break;
      }
      case "subagent_started": {
        const key = `${scope.workflowId}:${frame.data.subagent_id}`;
        const previous = openSubagent.get(key);
        if (previous) {
          dirty.add(previous.lifecycle.parentTurnNumber);
          idleGaps.close(timestamp);
          closedSubagents.push({
            ...previous.lifecycle,
            tone: "error",
            endTs: timestamp,
            endIndex: index,
            ongoing: false
          });
        }
        idleGaps.open(timestamp);
        openSubagent.set(key, {
          scopeKey: scope.key,
          lifecycle: {
            key: `subagent:${scope.workflowId}:${scope.parentTurnNumber}:${frame.data.subagent_id}`,
            source: "subagent",
            kind: "subagent",
            label: `${frame.data.agent_key} (${frame.data.subagent_id})`,
            detail: frame.data.workflow_id,
            tone: "model",
            startTs: timestamp,
            endTs: timestamp,
            startIndex: index,
            endIndex: index,
            ongoing: true,
            parentTurnNumber: scope.parentTurnNumber,
            workflowId: frame.data.workflow_id,
            subagentId: frame.data.subagent_id,
            ...(scope.role === "subagent" && { parentWorkflowId: scope.workflowId }),
            depth: 0
          }
        });
        break;
      }
      case "subagent_stopped": {
        const key = `${scope.workflowId}:${frame.data.subagent_id}`;
        const open = openSubagent.get(key);
        if (!open) break;
        // The row belongs to the turn it started in, which may be an earlier one.
        dirty.add(open.lifecycle.parentTurnNumber);
        closedSubagents.push({
          ...open.lifecycle,
          tone: "done",
          endTs: timestamp,
          endIndex: index,
          ongoing: false
        });
        idleGaps.close(timestamp);
        openSubagent.delete(key);
        break;
      }
      case "message_handler_error": {
        closeOpenSpansForTurn(scope, timestamp, index, "error", frame.data.message);
        break;
      }
    }
  }

  /**
   * One parent turn as drawn: its spans laned, with every span still open in it flushed
   * as ongoing, ending at the newest frame its scope has seen.
   *
   * The flush writes into a copy, so the fold itself only ever holds what the frames
   * said and the next call can carry on from it. closeSpan writes through `turns`, so
   * it is pointed at a map holding just the copy for the length of the flush.
   */
  function finalize(turnNumber: number, inputLength: number): TurnTimeline | undefined {
    const raw = turns.get(turnNumber);
    const copy = new Map<number, TurnTimeline>();
    if (raw) {
      copy.set(turnNumber, {
        ...raw,
        spans: [...raw.spans],
        subagentTurns: raw.subagentTurns.map((sub) => ({ ...sub, spans: [...sub.spans] }))
      });
    }
    const fold = turns;
    turns = copy;
    try {
      const flush = (map: ReadonlyMap<unknown, OpenSpan>, tone: SpanTone) => {
        for (const [, open] of map) {
          if (open.scope.parentTurnNumber !== turnNumber) continue;
          const lastSeen = lastSeenByScope.get(open.scope.key);
          closeSpan(
            open,
            Math.max(lastSeen?.timestamp ?? open.startTs, open.startTs),
            lastSeen?.index ?? inputLength,
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
      flush(openEvaluation, "approval");
      flush(openCallback, "approval");
    } finally {
      turns = fold;
    }

    const turn = copy.get(turnNumber);
    if (!turn || turn.turnNumber <= 0) return undefined;
    const packed = laneSpans(turn.spans);
    const subagentTurns = turn.subagentTurns
      .filter((subagentTurn) => subagentTurn.turnNumber > 0 && subagentTurn.spans.length > 0)
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
  }

  let orderedTurns: TurnTimeline[] = [];

  return (input) => {
    if (input.length < seen || (seen > 0 && input[seen - 1] !== last)) {
      turns = new Map();
      openModel = new Map();
      openTool = new Map();
      openApproval = new Map();
      openCallback = new Map();
      openEvaluation = new Map();
      openSubagent = new Map();
      closedSubagents = [];
      lastSeenByScope = new Map();
      previewByScope = new Map();
      seen = 0;
      highestOffset = null;
      pendingGap = false;
      built = new Map();
      orderedTurns = [];
      runPoints.length = 0;
      workflowRows.clear();
      idleGaps.clear();
    }
    if (input.length === seen && seen > 0) {
      return { turns: orderedTurns, workflow: workflowProjection(workflowSubagents()) };
    }
    dirty.clear();
    for (let position = seen; position < input.length; position += 1) foldFrame(input[position]!, position);
    seen = input.length;
    last = input[seen - 1];
    for (const map of [openModel, openTool, openApproval, openEvaluation, openCallback]) {
      for (const open of map.values()) dirty.add(open.scope.parentTurnNumber);
    }
    const changed = [...dirty];
    let added = false;
    for (const turnNumber of changed) {
      const turn = finalize(turnNumber, input.length);
      if (!turn) continue;
      added ||= !built.has(turnNumber);
      built.set(turnNumber, turn);
    }
    dirty.clear();
    orderedTurns = added
      ? [...built.values()].sort((a, b) => a.turnNumber - b.turnNumber)
      : orderedTurns.map((turn) => built.get(turn.turnNumber)!);
    const subagents = workflowSubagents();
    for (const turnNumber of changed) {
      const turn = built.get(turnNumber);
      if (turn) workflowRows.set(turnNumber, workflowRowsFor(turn, subagents));
    }
    if (idleGaps.needsRebuild) {
      idleGaps.rebuild(workflowRows.rows());
    }
    return { turns: orderedTurns, workflow: workflowProjection(subagents) };
  };

  function workflowProjection(subagents: WorkflowSubagentLifecycle[]): WorkflowTimeline {
    return {
      subagents,
      points: runPoints,
      bounds: idleGaps.bounds,
      gaps: idleGaps.gaps(),
      ongoing: openSpanCount() > 0,
      rowCount: workflowRows.rowCount,
      rowAt: (index) => workflowRows.rowAt(index),
      rows: () => workflowRows.flat()
    };
  }

  function openSpanCount(): number {
    return (
      openModel.size +
      openTool.size +
      openApproval.size +
      openEvaluation.size +
      openCallback.size +
      openSubagent.size
    );
  }

  function workflowSubagents(): WorkflowSubagentLifecycle[] {
    const ongoing = [...openSubagent.values()].map(({ lifecycle, scopeKey }) => {
      const lastSeen = lastSeenByScope.get(scopeKey);
      return {
        ...lifecycle,
        endTs: Math.max(lifecycle.startTs, lastSeen?.timestamp ?? lifecycle.startTs),
        endIndex: lastSeen?.index ?? lifecycle.startIndex
      };
    });
    return [...closedSubagents, ...ongoing].sort(
      (a, b) => a.parentTurnNumber - b.parentTurnNumber || a.startIndex - b.startIndex
    );
  }
}

interface RowNode {
  row: WorkflowTimelineRow;
  children: RowNode[];
  /** Where it sits among its siblings: a tool call's first request, not where its tool ran. */
  order: number;
  /** A subagent's workflow, whose own rows go under it. */
  agent?: string;
}

const rowOrder = (a: RowNode, b: RowNode) =>
  a.order - b.order ||
  a.row.startIndex - b.row.startIndex ||
  a.row.endIndex - b.row.endIndex ||
  a.row.kind.localeCompare(b.row.kind);

/**
 * One agent's steps as a forest. A tool call's approval, approval checks and callback share
 * its `tool_id`, so they sit under it, and a check made while its gate was open sits under
 * the gate. With no tool span (a denied call) the gate heads the group. Nothing else nests:
 * the protocol does not say which model call asked for a tool, so a tool stays beside it.
 */
function toolForest(rows: WorkflowTimelineRow[]): RowNode[] {
  const roots: RowNode[] = [];
  const groups = new Map<string, RowNode[]>();
  for (const row of rows) {
    const node: RowNode = { row, children: [], order: row.startIndex };
    if (row.toolId == null) {
      roots.push(node);
      continue;
    }
    const key = `${row.subagentTurnNumber ?? ""}:${row.toolId}`;
    const group = groups.get(key);
    if (group) group.push(node);
    else groups.set(key, [node]);
  }
  for (const nodes of groups.values()) {
    nodes.sort(rowOrder);
    const gate = nodes.find(({ row }) => row.waitType === "approval");
    const head = nodes.find(({ row }) => row.kind === "tool") ?? gate ?? nodes[0]!;
    for (const node of nodes) {
      if (node === head) continue;
      const underGate =
        gate && node.row.kind === "evaluation" && gate.row.startIndex <= node.row.startIndex;
      (underGate ? gate : head).children.push(node);
      /* Under its own tool the name would only repeat the row above. */
      const wait = node.row.waitType;
      if (head.row.kind === "tool" && (wait === "approval" || wait === "callback")) {
        node.row = { ...node.row, label: wait };
      }
    }
    head.order = nodes[0]!.order;
    roots.push(head);
  }
  return roots;
}

/**
 * One parent turn's rows in tree preorder: each agent's steps and the subagents it started,
 * in start order, with each subagent's own rows under it, to any depth.
 */
function workflowRowsFor(
  turn: TurnTimeline,
  subagents: WorkflowSubagentLifecycle[]
): WorkflowTimelineRow[] {
  const spanRow = (
    span: TimelineSpan,
    workflowId: string,
    subagentTurnNumber?: number
  ): WorkflowTimelineRow => ({
    ...span,
    key:
      subagentTurnNumber == null
        ? `parent:${turn.turnNumber}:span:${span.id}`
        : `subagent:${workflowId}:${turn.turnNumber}:${subagentTurnNumber}:span:${span.id}`,
    source: "span",
    parentTurnNumber: turn.turnNumber,
    workflowId,
    subagentTurnNumber,
    depth: 0
  });
  const lifecycles = subagents.filter((item) => item.parentTurnNumber === turn.turnNumber);
  const lifecycleNode = (row: WorkflowSubagentLifecycle): RowNode => ({
    row,
    children: [],
    order: row.startIndex,
    agent: row.workflowId
  });
  /** An agent's own steps and the subagents it started; the root agent's without a workflow. */
  const forest = (workflowId?: string): RowNode[] => [
    ...toolForest(
      workflowId == null
        ? turn.spans.map((span) => spanRow(span, "parent"))
        : turn.subagentTurns
            .filter((child) => child.workflowId === workflowId)
            .flatMap((child) => child.spans.map((span) => spanRow(span, workflowId, child.turnNumber)))
    ),
    ...lifecycles.filter((item) => item.parentWorkflowId === workflowId).map(lifecycleNode)
  ];

  const rows: WorkflowTimelineRow[] = [];
  const placed = new Set<WorkflowTimelineRow>();
  const emit = (
    node: RowNode,
    depth: number,
    parentKey: string | undefined,
    lineage: readonly string[]
  ): void => {
    placed.add(node.row);
    const row = { ...node.row, depth, parentKey };
    rows.push(row);
    /* A workflow already on the path would recurse forever; a run cannot start itself. */
    const nested = node.agent != null && !lineage.includes(node.agent);
    const children = nested ? [...node.children, ...forest(node.agent)] : node.children;
    const next = nested ? [...lineage, node.agent!] : lineage;
    for (const child of children.sort(rowOrder)) emit(child, depth + 1, row.key, next);
  };
  for (const root of forest().sort(rowOrder)) emit(root, 0, undefined, []);
  /* Started by a subagent this turn never saw start, as when the stream opened past it: with
     no parent here to sit under, it is drawn at the top rather than dropped. */
  for (const lifecycle of lifecycles) {
    if (!placed.has(lifecycle)) emit(lifecycleNode(lifecycle), 0, undefined, []);
  }
  return rows;
}

/** A row worth the reader's eye: it failed, or it is still running or waiting on someone.
    Unlike the Logs pane's `logSignal`, a granted approval is not one: here nearly every
    tool has one, so counting it would open every tool. */
export function isSignalRow(row: Pick<WorkflowTimelineRow, "tone" | "ongoing">): boolean {
  return row.tone === "error" || row.ongoing;
}

/* A long tail is a span at least 3× the run's median leaf span and at least 10% of the run
   so far. A heuristic: raise `factor` if too many groups open on runs whose calls vary a
   lot, lower `share` to catch shorter stalls on long runs. */
export const LONG_TAIL = { factor: 3, share: 0.1 };

type FoldRow = Pick<WorkflowTimelineRow, "kind" | "tone" | "ongoing" | "startTs" | "endTs" | "depth">;

/** Whether a finished row of this kind is routine, so a group of them can fold away. A
    granted approval is: nearly every tool has one, so counting it would open every tool;
    one still waiting is running, and one that stalled is a long tail. A subagent is not,
    since it is a run of its own. */
function routineKind(row: Pick<WorkflowTimelineRow, "kind">): boolean {
  switch (row.kind) {
    case "model":
    case "tool":
    case "approval":
    case "evaluation":
      return true;
    case "subagent":
      return false;
    default: {
      const unhandled: never = row.kind;
      throw new Error(`Unknown timeline row kind ${String(unhandled)}`);
    }
  }
}

/**
 * Which parents start open: one running at `at` (the cursor's timestamp) or still open at
 * the newest frame, one with anything under it that was running then, failed, is not a
 * routine kind or is a long tail (LONG_TAIL), and the only parent in the run. Every other
 * parent holds finished routine calls and folds to its summary. Failures and long tails are
 * read from the whole run, as every row is drawn whole wherever the cursor is; only running
 * follows the cursor, so scrubbing back opens what was running then. Rows are in preorder,
 * so a row's descendants are the deeper rows right after it, and one pass from the end finds
 * what is under each row.
 */
export function openByDefault(rows: readonly FoldRow[], at: number = Number.POSITIVE_INFINITY): boolean[] {
  return Array.from(openAt(rows, foldBasis(rows), at), Boolean);
}

/** What the default fold reads of a run apart from the cursor, so a scrub reads it once. */
interface FoldBasis {
  /** Each row's parent's index, or -1. */
  parentOf: Int32Array;
  isParent: Uint8Array;
  /** Open wherever the cursor is: an ongoing row, or one over a row that is interesting
      apart from running at the cursor, or the only parent. */
  open: Uint8Array;
}

function foldBasis(rows: readonly FoldRow[]): FoldBasis {
  const parentOf = new Int32Array(rows.length);
  const isParent = new Uint8Array(rows.length);
  const ancestors: number[] = [];
  for (let index = 0; index < rows.length; index += 1) {
    const depth = rows[index]!.depth;
    while (ancestors.length > 0 && rows[ancestors.at(-1)!]!.depth >= depth) ancestors.pop();
    parentOf[index] = ancestors.at(-1) ?? -1;
    ancestors.push(index);
    isParent[index] = (rows[index + 1]?.depth ?? -1) > depth ? 1 : 0;
  }
  const elapsed = (row: FoldRow) => row.endTs - row.startTs;
  const leaves = Float64Array.from(rows.filter((_, index) => !isParent[index]), elapsed).sort();
  const median = leaves.length > 0 ? leaves[Math.floor((leaves.length - 1) / 2)]! : 0;
  const runStart = rows.reduce((first, row) => Math.min(first, row.startTs), Number.POSITIVE_INFINITY);
  const runEnd = rows.reduce((last, row) => Math.max(last, row.endTs), runStart);
  const tail = Math.max(LONG_TAIL.factor * median, LONG_TAIL.share * (runEnd - runStart));
  const longTail = (row: FoldRow) => elapsed(row) > 0 && elapsed(row) >= tail;
  const interesting = (row: FoldRow) =>
    row.ongoing || row.tone === "error" || !routineKind(row) || longTail(row);

  const parents = isParent.reduce((count, flag) => count + flag, 0);
  const open = new Uint8Array(rows.length);
  const stack: { depth: number; interesting: boolean }[] = [];
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const row = rows[index]!;
    let below = false;
    while (stack.length > 0 && stack.at(-1)!.depth > row.depth) below = stack.pop()!.interesting || below;
    open[index] = isParent[index] && (below || row.ongoing || parents === 1) ? 1 : 0;
    stack.push({ depth: row.depth, interesting: below || interesting(row) });
  }
  return { parentOf, isParent, open };
}

/** The basis plus every parent over a row running at `at`, itself included. */
function openAt(rows: readonly FoldRow[], basis: FoldBasis, at: number): Uint8Array {
  const open = basis.open.slice();
  const marked = new Uint8Array(rows.length);
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index]!;
    if (!(row.startTs <= at && at < row.endTs)) continue;
    for (let up = index; up >= 0 && !marked[up]; up = basis.parentOf[up]!) {
      marked[up] = 1;
      if (basis.isParent[up]) open[up] = 1;
    }
  }
  return open;
}

/** What a folded row hides, by kind and most first: "3 tools · 2 models". */
export function foldSummary(children: readonly Pick<WorkflowTimelineRow, "kind" | "waitType">[]): string {
  const noun = (row: Pick<WorkflowTimelineRow, "kind" | "waitType">): [string, string] => {
    switch (row.kind) {
      case "model":
        return ["model", "models"];
      case "tool":
        return ["tool", "tools"];
      case "approval":
        return row.waitType === "callback" ? ["callback", "callbacks"] : ["approval", "approvals"];
      case "evaluation":
        return ["check", "checks"];
      case "subagent":
        return ["agent", "agents"];
      default: {
        const unhandled: never = row.kind;
        throw new Error(`Unknown timeline row kind ${String(unhandled)}`);
      }
    }
  };
  const counts = new Map<string, { count: number; forms: [string, string] }>();
  for (const child of children) {
    const forms = noun(child);
    const entry = counts.get(forms[0]) ?? { count: 0, forms };
    entry.count += 1;
    counts.set(forms[0], entry);
  }
  return [...counts.values()]
    .sort((a, b) => b.count - a.count)
    .map(({ count, forms }) => `${count} ${forms[count === 1 ? 0 : 1]}`)
    .join(" · ");
}

/**
 * The rows left showing once folded rows are folded, which rows are folded, and, per folded
 * row, how many direct children it hides and what they are. A row folds when the reader
 * folds it (`folds`), which always wins, or by default when `openByDefault` as of `at` does
 * not open it. One pass from the start skips the folded.
 *
 * A timeline's rows are fixed once built, so the cursor-free half of the default is read
 * once per timeline, and a cursor move that opens and folds the same rows gets back the same
 * object, which leaves everything derived from it standing.
 */
export function shownWorkflowRows(
  workflow: Pick<WorkflowTimeline, "rowCount" | "rowAt" | "rows">,
  folds: ReadonlyMap<string, boolean> = new Map(),
  at: number = Number.POSITIVE_INFINITY
): ShownWorkflowRows {
  let entry = shownRowsCache.get(workflow);
  if (!entry) {
    const rows = workflow.rows?.().slice() ?? Array.from({ length: workflow.rowCount }, (_, index) => workflow.rowAt(index)!);
    entry = { rows, basis: foldBasis(rows) };
    shownRowsCache.set(workflow, entry);
  }
  const { rows, basis, last } = entry;
  const open = openAt(rows, basis, at);
  const readerFolds = JSON.stringify([...folds]);
  if (last && last.folds === readerFolds && last.open.every((flag, index) => flag === open[index])) {
    return last.shown;
  }
  const indexes: number[] = [];
  const hidden: Record<number, number> = {};
  const summaries: Record<number, string> = {};
  const collapsed = new Set<string>();
  for (let index = 0; index < rows.length; ) {
    const row = rows[index]!;
    indexes.push(index);
    let next = index + 1;
    const parent = (rows[next]?.depth ?? -1) > row.depth;
    if (parent && (folds.get(row.key) ?? !open[index])) {
      collapsed.add(row.key);
      const children: WorkflowTimelineRow[] = [];
      while (next < rows.length) {
        const depth = rows[next]!.depth;
        if (depth <= row.depth) break;
        if (depth === row.depth + 1) children.push(rows[next]!);
        next += 1;
      }
      hidden[index] = children.length;
      summaries[index] = foldSummary(children);
    }
    index = next;
  }
  const shown = { indexes, hidden, summaries, collapsed };
  entry.last = { open, folds: readerFolds, shown };
  return shown;
}

type ShownWorkflowRows = {
  indexes: number[];
  hidden: Record<number, number>;
  summaries: Record<number, string>;
  collapsed: Set<string>;
};

const shownRowsCache = new WeakMap<
  object,
  { rows: WorkflowTimelineRow[]; basis: FoldBasis; last?: { open: Uint8Array; folds: string; shown: ShownWorkflowRows } }
>();

class WorkflowRowBlocks {
  private turnNumbers: number[] = [];
  private blocks: WorkflowTimelineRow[][] = [];
  private positions = new Map<number, number>();
  private counts = new FenwickCounts();
  /* Every row in order, current up to the first block changed since it was last read. */
  private flatRows: WorkflowTimelineRow[] = [];
  private staleFrom = 0;

  get rowCount(): number {
    return this.counts.total;
  }

  clear(): void {
    this.turnNumbers = [];
    this.blocks = [];
    this.positions.clear();
    this.counts = new FenwickCounts();
    this.flatRows = [];
    this.staleFrom = 0;
  }

  set(turnNumber: number, rows: WorkflowTimelineRow[]): void {
    const existing = this.positions.get(turnNumber);
    if (existing != null) {
      this.counts.replace(existing, rows.length);
      this.blocks[existing] = rows;
      this.staleFrom = Math.min(this.staleFrom, existing);
      return;
    }
    const position = this.turnNumbers.length;
    this.turnNumbers.push(turnNumber);
    this.blocks.push(rows);
    this.positions.set(turnNumber, position);
    this.counts.append(rows.length);
  }

  rowAt(index: number): WorkflowTimelineRow | undefined {
    if (index < 0 || index >= this.rowCount) return undefined;
    const block = this.counts.indexAt(index);
    return this.blocks[block]?.[index - this.counts.prefix(block)];
  }

  *rows(): Iterable<WorkflowTimelineRow> {
    for (const block of this.blocks) yield* block;
  }

  /** Every row in order, rebuilt from the first changed block on: a live run changes its last. */
  flat(): readonly WorkflowTimelineRow[] {
    if (this.staleFrom < this.blocks.length) {
      this.flatRows.length = this.counts.prefix(this.staleFrom);
      for (let block = this.staleFrom; block < this.blocks.length; block += 1) {
        for (const row of this.blocks[block]!) this.flatRows.push(row);
      }
    }
    this.staleFrom = this.blocks.length;
    return this.flatRows;
  }
}

const MAX_IDLE_GAP_CANDIDATES = 16;
const IDLE_GAP_THRESHOLD = 0.1;
/* Heuristic: a stretch some span runs through, with no span starting or ending in it, is
   idle once it is a minute long, as a wait on a person or a subagent left open is. Shorter,
   it is one call's own duration, and folding it would break up every short run. */
const QUIET_SPAN_SECONDS = 60;

/**
 * Idle is measured by activity, not coverage: the stretches between one span edge and the
 * next. One with nothing running folds at the threshold, as before; one that a long or
 * still-open span runs through folds too once it is also `QUIET_SPAN_SECONDS` long, so that
 * span draws across the break instead of holding the whole stretch open.
 */
class WorkflowIdleGapTracker {
  private active = 0;
  private lastActivity: number | null = null;
  private candidates: CollapsedGap[] = [];
  private lastTimestamp = Number.NEGATIVE_INFINITY;
  private runStart = Number.POSITIVE_INFINITY;
  private runEnd = Number.NEGATIVE_INFINITY;
  needsRebuild = false;

  get bounds(): WorkflowTimelineBounds {
    return {
      startTs: Number.isFinite(this.runStart) ? this.runStart : 0,
      endTs: Number.isFinite(this.runEnd) ? this.runEnd : 0
    };
  }

  clear(): void {
    this.active = 0;
    this.lastActivity = null;
    this.candidates = [];
    this.lastTimestamp = Number.NEGATIVE_INFINITY;
    this.runStart = Number.POSITIVE_INFINITY;
    this.runEnd = Number.NEGATIVE_INFINITY;
    this.needsRebuild = false;
  }

  observe(timestamp: number): void {
    if (!Number.isFinite(timestamp)) return;
    if (timestamp < this.lastTimestamp) this.needsRebuild = true;
    this.lastTimestamp = Math.max(this.lastTimestamp, timestamp);
    this.runStart = Math.min(this.runStart, timestamp);
    this.runEnd = Math.max(this.runEnd, timestamp);
    this.lastActivity ??= timestamp;
  }

  open(timestamp: number): void {
    this.edge(timestamp);
    this.active += 1;
  }

  close(timestamp: number): void {
    if (this.active === 0) return;
    this.edge(timestamp);
    this.active -= 1;
  }

  gaps(): CollapsedGap[] {
    const tail = this.lastActivity == null ? [] : quietGap(this.lastActivity, this.bounds.endTs, this.active > 0);
    return selectIdleGaps([...this.candidates, ...tail], this.bounds);
  }

  rebuild(rows: Iterable<WorkflowTimelineRow>): void {
    const stretches = quietStretches(rows, this.bounds);
    this.active = stretches.active;
    this.lastActivity = stretches.lastActivity;
    this.candidates = longestIdleGaps(stretches.gaps);
    this.needsRebuild = false;
  }

  private edge(timestamp: number): void {
    if (this.lastActivity != null) {
      const gap = quietGap(this.lastActivity, timestamp, this.active > 0);
      if (gap.length > 0) this.candidates = longestIdleGaps([...this.candidates, ...gap]);
    }
    this.lastActivity = timestamp;
  }
}

function idleGap(startTs: number, endTs: number): CollapsedGap {
  return { key: `idle:${startTs}:${endTs}`, startTs, endTs };
}

/** The stretch as a gap if it may fold: empty, or quiet for long enough under a span. */
function quietGap(startTs: number, endTs: number, covered: boolean): CollapsedGap[] {
  return endTs > startTs && (!covered || endTs - startTs >= QUIET_SPAN_SECONDS)
    ? [idleGap(startTs, endTs)]
    : [];
}

/** Every stretch between span edges that may fold, from the rows alone. An ongoing row has
    no end, so it covers everything after its start. */
function quietStretches(
  rows: Iterable<WorkflowTimelineRow>,
  bounds: WorkflowTimelineBounds
): { gaps: CollapsedGap[]; lastActivity: number; active: number } {
  const edges: Array<[timestamp: number, delta: number]> = [];
  for (const row of rows) {
    edges.push([row.startTs, 1]);
    if (!row.ongoing) edges.push([row.endTs, -1]);
  }
  edges.sort((a, b) => a[0] - b[0]);
  const gaps: CollapsedGap[] = [];
  let cursor = bounds.startTs;
  let active = 0;
  for (const [timestamp, delta] of edges) {
    gaps.push(...quietGap(cursor, timestamp, active > 0));
    cursor = Math.max(cursor, timestamp);
    active += delta;
  }
  return { gaps, lastActivity: cursor, active };
}

function longestIdleGaps(gaps: CollapsedGap[]): CollapsedGap[] {
  return gaps
    .filter(({ startTs, endTs }) => endTs > startTs)
    .sort(
      (a, b) =>
        b.endTs - b.startTs - (a.endTs - a.startTs) ||
        a.startTs - b.startTs
    )
    .slice(0, MAX_IDLE_GAP_CANDIDATES);
}

function selectIdleGaps(
  candidates: CollapsedGap[],
  bounds: WorkflowTimelineBounds
): CollapsedGap[] {
  const duration = Math.max(0, bounds.endTs - bounds.startTs);
  if (duration === 0) return [];
  return longestIdleGaps(candidates)
    .filter(
      (gap) =>
        gap.endTs - gap.startTs >= duration * IDLE_GAP_THRESHOLD &&
        /* Folding the whole run would leave nothing to draw beside the break. */
        (gap.startTs > bounds.startTs || gap.endTs < bounds.endTs)
    )
    .sort((a, b) => a.startTs - b.startTs);
}

export function findWorkflowIdleGaps(
  rows: readonly WorkflowTimelineRow[],
  bounds: WorkflowTimelineBounds
): CollapsedGap[] {
  const { gaps, lastActivity, active } = quietStretches(rows, bounds);
  return selectIdleGaps([...gaps, ...quietGap(lastActivity, bounds.endTs, active > 0)], bounds);
}

/**
 * The run's gaps with the stretch from its newest frame on to now folded as well, by the same
 * rule measured to now, while an open session has something running. The tail keeps one key
 * as now moves, so a reader's unfold of it holds until the next frame.
 */
export function liveIdleGaps(
  gaps: readonly CollapsedGap[],
  bounds: WorkflowTimelineBounds,
  nowTs: number | null
): readonly CollapsedGap[] {
  if (nowTs == null || nowTs <= bounds.endTs) return gaps;
  const last = gaps.at(-1);
  const joins = last?.endTs === bounds.endTs;
  const startTs = joins ? last!.startTs : bounds.endTs;
  const tail = selectIdleGaps(quietGap(startTs, nowTs, true), { startTs: bounds.startTs, endTs: nowTs });
  if (tail.length === 0) return gaps;
  return [...(joins ? gaps.slice(0, -1) : gaps), { ...tail[0]!, key: `idle:${startTs}:now` }];
}

class FenwickCounts {
  private values: number[] = [];
  private tree = [0];

  get total(): number {
    return this.prefix(this.values.length);
  }

  append(value: number): void {
    const index = this.values.length + 1;
    this.values.push(value);
    const start = index - (index & -index);
    this.tree[index] = value + this.prefix(index - 1) - this.prefix(start);
  }

  replace(position: number, value: number): void {
    const delta = value - this.values[position]!;
    this.values[position] = value;
    for (let index = position + 1; index < this.tree.length; index += index & -index) {
      this.tree[index] += delta;
    }
  }

  prefix(length: number): number {
    let sum = 0;
    for (let index = length; index > 0; index -= index & -index) sum += this.tree[index]!;
    return sum;
  }

  indexAt(offset: number): number {
    let index = 0;
    let sum = 0;
    for (let bit = 2 ** Math.floor(Math.log2(this.values.length)); bit; bit >>= 1) {
      const next = index + bit;
      if (next <= this.values.length && sum + this.tree[next]! <= offset) {
        index = next;
        sum += this.tree[next]!;
      }
    }
    return index;
  }
}

export function timestampAtIndex(
  points: readonly WorkflowTimelinePoint[],
  index: number
): number | null {
  if (points.length === 0) return null;
  if (index <= points[0]![0]) return points[0]![1];
  if (index >= points.at(-1)![0]) return points.at(-1)![1];
  let lo = 1;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid]![0] < index) lo = mid + 1;
    else hi = mid;
  }
  const [fromIndex, fromTs] = points[lo - 1]!;
  const [toIndex, toTs] = points[lo]!;
  return fromTs + ((index - fromIndex) / (toIndex - fromIndex)) * (toTs - fromTs);
}

export function indexAtTimestamp(
  points: readonly WorkflowTimelinePoint[],
  timestamp: number
): number | null {
  if (points.length === 0) return null;
  if (timestamp <= points[0]![1]) return points[0]![0];
  if (timestamp >= points.at(-1)![1]) return points.at(-1)![0];
  let lo = 1;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid]![1] < timestamp) lo = mid + 1;
    else hi = mid;
  }
  const [fromIndex, fromTs] = points[lo - 1]!;
  const [toIndex, toTs] = points[lo]!;
  return Math.round(
    toTs === fromTs
      ? toIndex
      : fromIndex + ((timestamp - fromTs) / (toTs - fromTs)) * (toIndex - fromIndex)
  );
}

function spanOrder(a: TimelineSpan, b: TimelineSpan): number {
  return a.startTs - b.startTs || a.startIndex - b.startIndex || a.endTs - b.endTs;
}

/** Top to bottom in every track, so a turn is compared to a turn lane by lane. */
const LANE_ORDER: SpanKind[] = ["model", "tool", "approval", "evaluation"];

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
  const points = playheadPoints(turn);
  if (points.length === 0) return null;

  const first = points[0];
  const last = points[points.length - 1];
  if (viewIndex < first[0] || viewIndex > last[0]) return null;

  /* The first point at or past the cursor, by binary search: the waterfall asks this of
     every row on every step, and a 50,000-span turn has 100,000 points. */
  let lo = 1;
  let hi = points.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] < viewIndex) lo = mid + 1;
    else hi = mid;
  }
  let ts = first[1];
  if (lo < points.length) {
    const [fromIndex, fromTs] = points[lo - 1];
    const [toIndex, toTs] = points[lo];
    ts =
      toIndex === fromIndex
        ? toTs
        : fromTs + ((viewIndex - fromIndex) / (toIndex - fromIndex)) * (toTs - fromTs);
  }

  return Math.min(1, Math.max(0, (ts - turn.startTs) / turnScale(turn)));
}

/** The event index nearest a position along one turn's wall-clock. */
export function indexAtFraction(turn: TurnTimeline, fraction: number): number | null {
  const points = playheadPoints(turn);
  if (points.length === 0) return null;

  const first = points[0]!;
  const last = points[points.length - 1]!;
  const ts = turn.startTs + Math.min(1, Math.max(0, fraction)) * turnScale(turn);
  if (ts <= first[1]) return first[0];
  if (ts >= last[1]) return last[0];

  let lo = 1;
  let hi = points.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid]![1] < ts) lo = mid + 1;
    else hi = mid;
  }
  const [fromIndex, fromTs] = points[lo - 1]!;
  const [toIndex, toTs] = points[lo]!;
  return Math.round(
    toTs === fromTs
      ? toIndex
      : fromIndex + ((ts - fromTs) / (toTs - fromTs)) * (toIndex - fromIndex)
  );
}

/* A built turn is never written to again — the fold hands out a new object when a turn
   changes — so its sorted points can be kept for as long as the turn is. */
const pointsByTurn = new WeakMap<TurnTimeline, Array<[index: number, ts: number]>>();

function playheadPoints(turn: TurnTimeline): Array<[index: number, ts: number]> {
  let points = pointsByTurn.get(turn);
  if (points) return points;
  points = [];
  for (const span of [...turn.spans, ...turn.subagentTurns.flatMap((sub) => sub.spans)]) {
    points.push([span.startIndex, span.startTs], [span.endIndex, span.endTs]);
  }
  points.sort((a, b) => a[0] - b[0]);
  pointsByTurn.set(turn, points);
  return points;
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
export function buildStepBoundaries(timeline: Pick<StepTimeline, "turns">): number[] {
  /* The fold hands back a settled turn as the same object, so a live append only reads
     the turns it changed; turns are joined as they are while each starts past the last. */
  const out: number[] = [];
  for (const turn of timeline.turns) {
    let own = boundariesByTurn.get(turn);
    if (!own) {
      own = turnBoundaries([turn]);
      boundariesByTurn.set(turn, own);
    }
    const first = own[0] === out.at(-1) ? 1 : 0;
    if (own.length > first && out.length > 0 && own[first]! < out.at(-1)!) return turnBoundaries(timeline.turns);
    for (let k = first; k < own.length; k += 1) out.push(own[k]!);
  }
  return out;
}

const boundariesByTurn = new WeakMap<TurnTimeline, number[]>();

function turnBoundaries(turns: readonly TurnTimeline[]): number[] {
  const indices = new Set<number>();
  for (const turn of turns) {
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
export function aggregateSpans(timeline: Pick<StepTimeline, "turns">): SpanAggregate[] {
  const totals = new Map<SpanKind, SpanAggregate>();
  for (const turn of timeline.turns) {
    addTurnAggregate(totals, turn.spans);
    for (const subagentTurn of turn.subagentTurns) {
      addTurnAggregate(totals, subagentTurn.spans);
    }
  }
  /* Display order, and also a WHITELIST: a kind missing from this list is dropped from the
     rollup entirely, with no type error to say so. Every SpanKind belongs here.
     "evaluation" follows "approval" because it is the check nested inside that gate. */
  const order: SpanKind[] = ["model", "tool", "approval", "evaluation"];
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

/* Which kind claims a stretch of time both cover, for the EXCLUSIVE rollup.
 *
 * "evaluation" outranks "approval" because it is nested inside it: an automatic check
 * runs while the gate it belongs to is open, and charging that window to "approval"
 * would put it back in the same bucket as the human wait — exactly the conflation the
 * separate kind exists to undo. Ranked this way, a gate's rollup reads as "approval
 * check 0.4s, approval 12s", and the second number is the time a person took. */
const PRIORITY: SpanKind[] = ["evaluation", "approval", "tool", "model"];

/**
 * Each stretch between two span edges, charged to the highest-priority kind open across it.
 *
 * A sweep over the sorted edges, counting open spans per kind. It was a filter of every
 * span at every edge, which is quadratic in the turn: a 50,000-span turn never finished.
 * A span that ends at or before it starts covers nothing and is left out of the count.
 */
function exclusiveTurnSegments(spans: TimelineSpan[]): { kind: SpanKind; seconds: number }[] {
  const edges: Array<[ts: number, kind: SpanKind, delta: number]> = [];
  for (const span of spans) {
    if (span.endTs > span.startTs) edges.push([span.startTs, span.kind, 1], [span.endTs, span.kind, -1]);
  }
  edges.sort((a, b) => a[0] - b[0]);
  const open: Record<SpanKind, number> = { evaluation: 0, approval: 0, tool: 0, model: 0 };
  const segments: { kind: SpanKind; seconds: number }[] = [];
  for (let i = 0; i < edges.length; ) {
    const startTs = edges[i]![0];
    for (; i < edges.length && edges[i]![0] === startTs; i += 1) open[edges[i]![1]] += edges[i]![2];
    const kind = PRIORITY.find((k) => open[k] > 0);
    if (kind && i < edges.length) segments.push({ kind, seconds: edges[i]![0] - startTs });
  }
  return segments;
}

/** A lane too crowded for a bar per span, drawn as one strip instead. */
export interface LaneStrip {
  lane: number;
  kind: SpanKind;
  /** The lane's spans in start order, which is also end order: laneSpans never lets a lane overlap itself. */
  spans: TimelineSpan[];
  /** SVG path data over a `width` × 20 box: each run of pixels holding a span, a failure, a span still running. */
  fill: string;
  error: string;
  ongoing: string;
  errors: number;
}

/** Height of a lane's bar, in px; the strip's path is drawn in a box this tall. */
export const BAR_PX = 20;

/**
 * The narrowest a lane's bars may average before the lane becomes a strip. Narrower than
 * this a bar carries no label and cannot be told from its neighbor, and a track of them
 * still costs a button each: 5,000 one-pixel bars took 300 ms to restyle on a turn jump.
 */
const MIN_BAR_PX = 4;

/**
 * The lanes of a track too dense to draw a span at a time, each binned into per-pixel buckets.
 *
 * Past one span per MIN_BAR_PX a bar per span stops adding anything a reader can see and
 * keeps costing a button each: a 50,000-span turn was 50,000 of them, every one restyled on every
 * step of the cursor. A dense lane is three paths instead — where there are spans, where
 * one failed, where one is still running — and the lanes with room keep their bars. A lane
 * never overlaps itself, so marking each span's pixels costs the width plus the span count.
 */
export function denseLanes(
  spans: TimelineSpan[],
  turnStart: number,
  scale: number,
  widthPx: number
): LaneStrip[] {
  const width = Math.max(1, Math.round(widthPx));
  const byLane = new Map<number, TimelineSpan[]>();
  for (const span of spans) {
    const lane = byLane.get(span.lane);
    if (lane) lane.push(span);
    else byLane.set(span.lane, [span]);
  }
  const px = (ts: number) => Math.min(width - 1, Math.max(0, Math.floor(((ts - turnStart) / scale) * width)));
  const strips: LaneStrip[] = [];
  for (const [lane, laneSpans] of byLane) {
    if (laneSpans.length * MIN_BAR_PX <= width) continue;
    const buckets = new Uint8Array(width);
    let errors = 0;
    for (const span of laneSpans) {
      const flag = 1 | (span.tone === "error" ? 2 : 0) | (span.ongoing ? 4 : 0);
      if (span.tone === "error") errors += 1;
      for (let x = px(span.startTs), to = Math.max(x, px(span.endTs)); x <= to; x += 1) buckets[x] |= flag;
    }
    strips.push({
      lane,
      kind: laneSpans[0]!.kind,
      spans: laneSpans,
      fill: pixelRuns(buckets, 1),
      error: pixelRuns(buckets, 2),
      ongoing: pixelRuns(buckets, 4),
      errors
    });
  }
  return strips;
}

function pixelRuns(buckets: Uint8Array, flag: number): string {
  let d = "";
  for (let x = 0; x < buckets.length; ) {
    if (!(buckets[x]! & flag)) {
      x += 1;
      continue;
    }
    const from = x;
    while (x < buckets.length && buckets[x]! & flag) x += 1;
    d += `M${from} 0h${x - from}v${BAR_PX}h${from - x}z`;
  }
  return d;
}

/**
 * The longest span in a strip's lane that overlaps `[fromTs, toTs]`, or null when none does.
 *
 * What a pointer over a strip points at: a binary search to the first span ending inside
 * the window, then a walk while spans still start inside it, so a pixel holding a hundred
 * spans answers with the one that took the time.
 */
export function spanNear(spans: TimelineSpan[], fromTs: number, toTs: number): TimelineSpan | null {
  let lo = 0;
  let hi = spans.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (spans[mid]!.endTs < fromTs) lo = mid + 1;
    else hi = mid;
  }
  let best: TimelineSpan | null = null;
  for (let i = lo; i < spans.length && spans[i]!.startTs <= toTs; i += 1) {
    if (!best || spans[i]!.durationSeconds > best.durationSeconds) best = spans[i]!;
  }
  return best;
}
