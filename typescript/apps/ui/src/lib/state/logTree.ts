/**
 * The Logs pane's rows as a tree, and which of them are signal.
 *
 * The tree is the chat trace's steps (`traceSteps`): a call's request, gate, run and result share a
 * `tool_id`, a model call is its start and end, a subagent is its id. A step of one event stays a
 * plain row; a step of several becomes a head with its events under it. Two relations nest one step
 * under another: a call a Code Mode script made goes under that script's host, and every event of a
 * subagent's workflow goes under the step that started it. Both come from the rows alone.
 */
import type { StatusKind } from "$lib/components/primitives/StatusChip.svelte";
import { codeModeHostsByRow } from "$lib/state/codeModeNesting";
import type { ReplayLogRow, ReplayMarkerTone } from "$lib/state/replayLog";
import { traceSteps, type StepState, type TraceStep } from "$lib/state/replyStatus";

/* A step's glyph, in the chat trace and on a log head. */
export const STEP_GLYPHS: Record<StepState, StatusKind> = {
  requested: "tool",
  awaiting: "approval",
  running: "tool",
  done: "complete",
  failed: "error",
  rejected: "error"
};

/**
 * Why a row is worth the reader's eye, strongest first. A row with none is routine and drawn dim.
 * Read from what the log already says about a row, never its words: its tone, its actor, and the
 * marker the scrubber flags it with.
 */
export type LogSignal = ReplayMarkerTone | "approved";
const SIGNAL_RANK: readonly LogSignal[] = ["error", "approval", "queue", "approved"];

export function logSignal(row: ReplayLogRow): LogSignal | null {
  if (row.tone === "error" || row.actor === "error") return "error";
  if (row.marker) return row.marker;
  if (row.tone === "approval") return "approval";
  if (row.actor === "approval" && row.tone === "done") return "approved";
  return null;
}

export type LogLine =
  | { kind: "row"; id: string; row: ReplayLogRow; depth: number; signal: LogSignal | null; underStep: boolean }
  | {
      kind: "step";
      id: string;
      step: TraceStep;
      depth: number;
      /** The strongest signal of anything under it, so a folded head still says it. */
      signal: LogSignal | null;
      collapsed: boolean;
      /** Every row under it, to find the playhead inside a folded head. */
      rowIds: ReadonlySet<string>;
    };

interface StepNode {
  step: TraceStep;
  ordinal: number;
  children: StepNode[];
}

function stepTree(rows: readonly ReplayLogRow[]): StepNode[] {
  const hosts = codeModeHostsByRow(rows);
  const nodes = traceSteps(rows).map((step) => ({ step, ordinal: step.rows[0]!.ordinal, children: [] as StepNode[] }));
  const tools = new Map<string, StepNode>();
  const subagents = new Map<string, StepNode>();
  for (const node of nodes) {
    const first = node.step.rows[0]!;
    if (node.step.kind === "tool" && first.toolId) tools.set(`${first.workflowId ?? ""}:${first.toolId}`, node);
    if (node.step.kind !== "subagent") continue;
    for (const row of node.step.rows) {
      if (row.childWorkflowId && row.childWorkflowId !== row.workflowId) subagents.set(row.childWorkflowId, node);
    }
  }
  const roots: StepNode[] = [];
  for (const node of nodes) {
    const first = node.step.rows[0]!;
    const host = hosts.get(first.id);
    const parent =
      (host != null ? tools.get(`${first.workflowId ?? ""}:${host}`) : undefined) ??
      (first.workflowId != null ? subagents.get(first.workflowId) : undefined);
    /* Only under something that came first, which is also what keeps the tree acyclic. */
    if (parent && parent.ordinal < node.ordinal) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

const strongest = (signals: Iterable<LogSignal | null>): LogSignal | null => {
  let best: LogSignal | null = null;
  for (const signal of signals) {
    if (signal && (best == null || SIGNAL_RANK.indexOf(signal) < SIGNAL_RANK.indexOf(best))) best = signal;
  }
  return best;
};

/**
 * The tree drawn as lines. A head folds when the reader folds it, or, by default, when nothing
 * under it is signal: its routine events are one click away instead of a screen long. `expandAll`
 * overrides the default (a search or a filter must show what it matched) but not the reader.
 */
export function logLines(
  rows: readonly ReplayLogRow[],
  collapsed: Readonly<Record<string, boolean>> = {},
  expandAll = false
): LogLine[] {
  const rowLine = (row: ReplayLogRow, depth: number, underStep: boolean): LogLine => ({
    kind: "row",
    id: row.id,
    row,
    depth,
    signal: logSignal(row),
    underStep
  });
  const visit = (node: StepNode, depth: number, underStep: boolean): LogLine[] => {
    const { step } = node;
    if (step.rows.length === 1 && node.children.length === 0) return [rowLine(step.rows[0]!, depth, underStep)];
    const under = [
      ...step.rows.map((row) => ({ ordinal: row.ordinal, row, node: null })),
      ...node.children.map((child) => ({ ordinal: child.ordinal, row: null, node: child }))
    ]
      .sort((a, b) => a.ordinal - b.ordinal)
      .flatMap((item) => (item.row ? [rowLine(item.row, depth + 1, true)] : visit(item.node!, depth + 1, true)));
    const signal = strongest(under.map((line) => line.signal));
    const folded = collapsed[step.id] ?? (!expandAll && signal == null);
    const head: LogLine = {
      kind: "step",
      id: `step-${step.id}`,
      step,
      depth,
      signal,
      collapsed: folded,
      rowIds: new Set(under.flatMap((line) => (line.kind === "row" ? [line.row.id] : [...line.rowIds])))
    };
    return folded ? [head] : [head, ...under];
  };
  /* A subagent's event whose starter is not in this list (filtered out, or not yet arrived) still
     steps in once, as it always has. */
  return stepTree(rows).flatMap((root) => visit(root, root.step.rows[0]!.parentTurnNumber != null ? 1 : 0, false));
}
