import type { Edge, Node } from "@xyflow/svelte";
import type {
  AgentInterfaceFunction,
  AgentSseFrame,
  SubagentUnavailableCode,
  ToolId
} from "$lib/api/types";
import { kindFromState } from "$lib/components/flow/AgentStateNode.svelte";
import type { Metric } from "$lib/components/primitives/metrics";
import { STATUS_TONES } from "$lib/components/primitives/StatusChip.svelte";
import { formatTokens, summarizeCost, tokenFigure, UNREPORTED_TIP, type CostSummary } from "$lib/cost/pricing";
import { renderUserMessage } from "$lib/state/inboundMessageText";
import { handlerReply, outputSchemaTracker, streamingIsData, stripJsonFence } from "$lib/state/handlerReply";
import { UNKNOWN_TOOL_INPUT } from "$lib/state/logValue";
import { formatDuration } from "$lib/state/replayLog";
import { NO_THOUGHT_SUMMARY, thoughtDeltaText } from "$lib/state/thoughtSummary";

export type AgentNodeTone =
  | "neutral"
  | "agent"
  | "model"
  | "reasoning"
  | "tool"
  | "approval"
  | "done"
  | "error"
  | "queue";

export interface AgentNodeData {
  [key: string]: unknown;
  tone: AgentNodeTone;
  title: string;
  state: string;
  subtitle?: string;
  detail?: string;
  /** A runtime boundary's one line on why its detail is missing. */
  reason?: string;
  /** `detail` is a prose reply, drawn as markdown; decided by handlerReply's rule. */
  markdown?: boolean;
  active?: boolean;
  size?: "default" | "large" | "container";
  runtimeRole?: "parent" | "subagent";
  statusTone?: AgentNodeTone;
  /** Lit ports by handle id, each in a CSS color; set by markEdgeCues. */
  ports?: Record<string, string>;
  approvalPort?: boolean;
  approvalDecisionPort?: boolean;
  nodeWidth?: number;
  nodeHeight?: number;
  /** Overrides the result body's height, in px. See codeModeScriptHeight. */
  resultHeight?: number;
  flowGroup?: number;
  metrics?: Metric[];
  interfaces?: AgentInterfaceSummary[];
  /** Never empty — see contextFor(). The inspector renders these in order. */
  context?: AgentNodeContext[];
}

export interface AgentInterfaceSummary {
  name: string;
  description?: string;
}

/**
 * One readable body for the node inspector.
 *
 * A node card shows a bounded preview and says so; this is where the rest of it
 * lives. Built here rather than in the inspector because the inspector only ever
 * receives node DATA — it has no frames to reach back into, which is exactly why
 * it used to apologise for having nothing to show over a model interaction that
 * had streamed 3,515 characters: `detail` was the only field it read, and
 * nodeDataFor has never set one for that kind.
 */
export interface AgentNodeContext {
  label: string;
  text: string;
  /** `markdown` is prose a model or person wrote, drawn by MarkdownMessage. */
  kind: "text" | "code" | "json" | "markdown";
}

export interface AgentGraph {
  nodes: Node<AgentNodeData>[];
  edges: Edge[];
  activeTurn: number | null;
  status: "idle" | "running" | "replied" | "error";
  /** The latest turn's handler ended in an error. Kept past turn_end, which sets `status`
      back to idle. */
  failed: boolean;
  usage: CostSummary;
  /** Set on a subagent whose stream the merge gave up on: what the UI knows of it instead. */
  unreadable?: UnreadableChildState;
}

/**
 * What a subagent whose stream could not be read is doing, from what the UI knows without
 * that stream: the parent's reply, whether the parent stopped it, and Temporal's status. Never
 * from the free-text reason, and a missing stream alone is never an error.
 */
export type UnreadableChildState = "complete" | "error" | "closed" | "delegating" | "idle";

export interface UnreadableChildFacts {
  /** The parent's subagent_reply_received outcome for the child's latest turn. */
  reply?: "ok" | "error";
  stoppedByParent: boolean;
  /** Temporal's execution status, e.g. RUNNING or FAILED, when it has been asked. */
  executionStatus?: string;
  /** Closed as far as this console knows, which may be without a status. */
  closed: boolean;
}

export function unreadableChildState(facts: UnreadableChildFacts): UnreadableChildState {
  if (facts.reply === "ok") return "complete";
  if (facts.reply === "error") return "error";
  switch (facts.executionStatus) {
    case "FAILED":
    case "TIMED_OUT":
    case "TERMINATED":
      return "error";
    case "CANCELED":
    case "COMPLETED":
      return "closed";
  }
  if (facts.stoppedByParent || facts.closed) return "closed";
  return facts.executionStatus === "RUNNING" ? "delegating" : "idle";
}

const unreadableLooks: Record<UnreadableChildState, AgentNodeTone> = {
  complete: "done",
  error: "error",
  closed: "neutral",
  delegating: "agent",
  idle: "neutral"
};

/** The one line a boundary says about why its detail is missing. */
export function unreadableReasonLine(code: SubagentUnavailableCode | undefined, reason: string): string {
  if (code === undefined) return reason.trim() || "Its detail could not be read";
  switch (code) {
    case "closed_not_replayable":
      return "Finished before its detail could be replayed";
    case "read_failed":
      return "Its detail could not be read";
    case "ended_mid_turn":
      return "Ended mid-turn, before its detail was delivered";
    case "stalled":
      return "Stalled without delivering its turn";
    default: {
      const unhandled: never = code;
      throw new Error(`No reason line for ${String(unhandled)}`);
    }
  }
}

/** The inspector's account of a missing detail: the line, the merge's own words, and the facts. */
function unreadableContext(record: DelegationRecord, executionStatus: string | undefined): AgentNodeContext {
  const line = unreadableReasonLine(record.reasonCode, record.reason);
  const reported = record.reason.trim();
  return {
    label: "Why the detail is missing",
    text: [
      line,
      reported && reported !== line ? `Reported: ${reported}` : "",
      `Parent reply: ${record.reply ?? "none yet"}`,
      record.stoppedByParent ? "Stopped by its parent" : "",
      /* Asked once, on the marker, which lands before the parent's reply and any stop. */
      `Temporal status when last asked: ${executionStatus ?? "not known"}`
    ]
      .filter(Boolean)
      .join("\n"),
    kind: "text"
  };
}

export interface AgentGraphSource {
  workflowId: string;
  role: "parent" | "subagent";
  label: string;
  frames: AgentSseFrame[];
  parentWorkflowId?: string;
  subagentId?: string;
  agentKey?: string;
  agentInterface?: AgentInterfaceFunction[];
  stopped?: boolean;
  /** Temporal's execution status for this workflow, once something has asked. */
  executionStatus?: string;
}

interface ToolRuntime {
  id: ToolId;
  name: string;
  status: AgentNodeData["state"];
  tone: AgentNodeTone;
  statusTone?: AgentNodeTone;
  detail?: string;
  /* Kept apart from `detail`, which the card overwrites with whatever happened
     last. The inspector wants both halves of the call, and the arguments the
     model chose were being thrown away the moment the tool answered. */
  input?: string;
  output?: string;
  subtitle?: string;
  isCodeMode?: boolean;
  script?: string;
  parentToolId?: ToolId;
  flowGroup?: number;
  /** Consecutive identical failures stacked onto this node. Absent / 1 = not stacked. */
  retryCount?: number;
}

/** One run of an auto approval evaluator over one gated tool call. */
interface EvaluationRuntime {
  id: string;
  toolId: ToolId;
  toolName: string;
  evaluator: string;
  /* The card's status word. Past tense on purpose: AgentStateNode's kindFromState picks a
     chip by matching substrings like "approved" / "denied", so the raw verdicts "approve"
     and "deny" would both miss every rule and fall through to the neutral idle chip — a
     refused call rendering as if nothing had happened. */
  status: "evaluating" | "approved" | "denied" | "escalated" | "cancelled" | "failed";
  /* The verdict as the PROTOCOL spells it, kept apart from the display word above. The
     inspector is an audit surface and must show what the evaluator actually returned; the
     card's status word is chosen for the chip. Absent until it reaches one, and on a
     cancellation it usually never does. */
  verdict?: "approve" | "deny" | "escalate";
  tone: AgentNodeTone;
  statusTone?: AgentNodeTone;
  /** The evaluator's one-line reason, or the failure message. */
  detail?: string;
  /** Its structured reasoning, as JSON — the audit record the inspector renders. */
  reasoning?: string;
}

interface AgentGraphOptions {
  inputPlacement?: "external" | "runtime";
  showSubagentDispatch?: boolean;
  outputPlacement?: "external" | "runtime";
  agentInterface?: AgentInterfaceFunction[];
  embeddedToolGraphs?: AgentGraph[];
  /**
   * Draw the turn as it stands rather than as it happened: a settled tool leaves
   * the canvas. Off by default, so a caller that asks for nothing gets the
   * accumulated graph this file has always built.
   */
  focus?: boolean;
  /**
   * Tools that have settled but should still be drawn, so a call that finishes
   * says so before it goes rather than blinking out unacknowledged.
   *
   * Held here rather than computed from a clock, because this projection is also
   * what a scrub and a page-load replay run through: a tool that finished an hour
   * ago must not linger just because its frame is being read now. The caller owns
   * the timing and only ever puts a live settle in this set.
   */
  linger?: ReadonlySet<ToolId>;
  /** No edge is `animated`, so an open step's edge keeps its dash standing still. */
  reducedMotion?: boolean;
}

type RuntimeNodeId =
  | "input"
  | "model"
  | "reasoning"
  | "tool-container"
  | "subagent"
  | "output"
  | ToolRuntimeNodeId
  | ApprovalEvaluationNodeId;

type ToolRuntimeNodeId = `tool:${ToolId}`;

/* An automatic approval evaluator judging one gated call. A node of its OWN, not a state
   of the tool's node: the evaluator is separate work, running somewhere else, and folding
   it into the tool card meant clicking it showed the tool's input and output — the one
   thing it is not about. Keyed by evaluation_id, so a chain of evaluators over a single
   gated call draws as a chain. */
type ApprovalEvaluationNodeId = `approval:${string}`;

type LocalNodeId = RuntimeNodeId;

type EdgeKind =
  | "main"
  | "reasoning"
  | "approval"
  | "output";

interface NodeDimensions {
  width: number;
  height: number;
}

interface RuntimeLayout {
  positions: Map<RuntimeNodeId, { x: number; y: number }>;
  boundaryWidth: number;
  boundaryHeight: number;
}

interface GraphBounds {
  minX: number;
  minY: number;
  width: number;
  height: number;
}

interface EmbeddedToolPlacement {
  graph: AgentGraph;
  x: number;
  y: number;
}

interface EmbeddedToolLayout {
  dimensions: NodeDimensions;
  placements: EmbeddedToolPlacement[];
}

const stateNodeWidth = 230;
const largeStateNodeWidth = 255;
const stateNodeHeight = 130;
const largeStateNodeHeight = 150;
/**
 * What a card measures once it carries a Result region, which AgentStateNode
 * holds at a fixed height on purpose. Reserved here rather than measured,
 * because these positions are computed from data and never from the DOM — a
 * card taller than its reservation overlaps the runtime boundary it sits in.
 *
 * One number for every kind of result, including scripts: the region is one
 * fixed height whatever it holds, which is what replaced the old special case
 * that reserved 210px for nodes whose detail happened to parse as a script.
 */
const resultNodeHeight = 231;
const runtimeColumnGap = 45;
const runtimeRowGap = 45;
const modelReasoningGap = 24;
const embeddedToolPadding = 18;
const embeddedToolHeaderHeight = 116;
const embeddedToolGap = 32;
const codeModePadding = 18;
/**
 * A Code Mode host draws its own head and its script above the host calls laid
 * out inside it, so the header has to reserve room for both.
 *
 * The script used to get whatever was left of a flat 126px, which was 26px — one
 * clipped line of Python behind a scrollbar, on the card whose whole point is the
 * script it ran. It gets a normal card's result body instead, and the node data
 * carries that number to the CSS so the two cannot drift apart.
 */
const codeModeHeadHeight = 100;
const codeModeScriptHeight = 132;
const codeModeHeaderHeight = codeModeHeadHeight + codeModeScriptHeight;
/* Gap between the card that owns a gated call and the approval satellites hanging under
   it: directly below, so the single edge between them reads as a drop rather than as
   another step in the left-to-right runtime flow. */
const approvalEvaluationGapY = 48;
/* Side by side when one card has several gated calls in flight — a Code Mode host can. */
const approvalEvaluationGapX = 32;

const codeModeColumns = 2;
const codeModeColumnGap = 32;
const codeModeRowGap = 26;
const layout = {
  input: { x: 0, y: 245 },
  runtime: { x: 300, y: 20 },
  gridStartX: 340,
  gridStartY: 140,
  columns: 3,
  runtimePaddingX: 40,
  outputGap: 30
};

function runtimeRows(count: number): number {
  return Math.max(1, Math.ceil(count / layout.columns));
}

function runtimeColumns(count: number): number {
  return Math.max(1, Math.min(layout.columns, count));
}

function runtimeBoundaryWidth(count: number): number {
  const columns = runtimeColumns(count);
  return (
    layout.runtimePaddingX * 2 +
    columns * stateNodeWidth +
    (columns - 1) * runtimeColumnGap
  );
}

function runtimeBoundaryHeight(count: number): number {
  const rows = runtimeRows(count);
  return (
    layout.gridStartY -
    layout.runtime.y +
    rows * stateNodeHeight +
    (rows - 1) * runtimeRowGap +
    40
  );
}

/**
 * What `.workflow-head` occupies in AgentWorkflowNode, mirrored here for the same
 * reason embeddedToolHeaderHeight is: this file computes geometry from data and
 * never measures the DOM. Measured at 88px in a browser — its `min-height`, which
 * binds even in the variant that fills it most (title, subtitle and an ACCEPTS
 * row), and the 120px above the first card is this plus 32px of breathing room.
 *
 * It is also the whole height of a boundary with nothing inside it, because the
 * insets on either side of the card grid have no grid to sit around.
 */
const runtimeHeaderHeight = 88;
/** What `.workflow-reason` adds to that header: its 6px gap and two clamped lines of 11px at 1.35. */
const runtimeReasonHeight = 36;

function outputPosition(boundaryWidth: number): { x: number; y: number } {
  return {
    x: layout.runtime.x + boundaryWidth + layout.outputGap,
    y: layout.input.y
  };
}

function summarizeAgentInterface(
  agentInterface: AgentInterfaceFunction[] | undefined
): AgentInterfaceSummary[] {
  return (agentInterface ?? [])
    .map((item) => ({
      name: item.name.trim(),
      description: item.description.replace(/\s+/g, " ").trim()
    }))
    .filter((item) => item.name);
}

function node(
  id: string,
  position: { x: number; y: number },
  data: AgentNodeData,
  type = "agentState"
): Node<AgentNodeData> {
  return {
    id,
    type,
    position,
    data,
    /* The whole title, which the card may have to clip, and the state it is in. */
    ariaLabel: `${data.title}, ${data.state}`,
    draggable: false,
    selectable: type !== "agentWorkflow",
    zIndex: type === "agentWorkflow" ? 0 : 10
  };
}

/* xyflow's default bezier, colored by its kind (the variables live in
   AgentStateFlow), with no marker: where it meets a card is drawn by that
   card's port, and which way it runs by the left-to-right layout. A wrap onto
   the next row is xyflow's square smoothstep instead, so it drops straight
   into the port rather than sweeping in sideways. What state the edge is in is
   added by markEdgeCues. */
function edge(
  id: string,
  source: string,
  target: string,
  options: {
    sourceHandle?: string;
    targetHandle?: string;
    kind?: EdgeKind;
    wrap?: boolean;
  } = {}
): Edge {
  const kind = options.kind ?? "main";
  return {
    id,
    source,
    target,
    sourceHandle: options.sourceHandle ?? "source-right",
    targetHandle: options.targetHandle ?? "target-left",
    class: `edge-${kind}`,
    zIndex: 1,
    ...(options.wrap ? { type: "smoothstep", pathOptions: { borderRadius: 0 } } : {})
  };
}

type EdgeState = "delivered" | "flowing" | "waiting" | "unreached" | "failed";

/* Pattern says what kind of flow it is, color says whether it needs attention.
   No color keeps the edge's own kind color, on the stroke and the target port.
   An animated edge is still open, as the timeline's open spans are: it takes
   xyflow's `animated` class, which AgentStateFlow draws as the timeline's
   marching dash, and under reduced motion keeps that dash standing still. */
const edgeLooks: Record<EdgeState, { color?: string; pattern?: string; animated?: true }> = {
  delivered: {},
  flowing: { animated: true },
  waiting: { color: "var(--live)", animated: true },
  unreached: { color: "var(--text-4)", pattern: "stroke-dasharray: 1 4; stroke-linecap: round" },
  failed: { color: "var(--error)" }
};

/* Read through the card's own kindFromState and STATUS_TONES, so an edge can
   only say what the card it points at says: red into a red card, amber into an
   amber one, moving only into a card that is still working or waiting. */
function edgeStateOf(data: AgentNodeData): EdgeState {
  const tone = STATUS_TONES[kindFromState(data.state, data.statusTone ?? data.tone)];
  if (tone === "error") return "failed";
  if (tone === "live") return "waiting";
  if (/streaming|running|delegating/i.test(data.state)) return "flowing";
  return tone === "success" ? "delivered" : "unreached";
}

/* Each edge takes the state of the step it leads into. A step the run has
   already gone past counts as delivered whatever it now says: the model card
   reads "idle" again once a turn ends, and the edge into it was still used. */
function markEdgeCues(
  nodes: Node<AgentNodeData>[],
  edges: Edge[],
  reducedMotion: boolean
): Pick<AgentGraph, "nodes" | "edges"> {
  const states = new Map(nodes.map((item) => [item.id, edgeStateOf(item.data)]));
  /* Repeated until nothing changes, so a pass costs edges × chain length. */
  for (let changed = true; changed; ) {
    changed = false;
    for (const item of edges) {
      const next = states.get(item.target);
      if (states.get(item.source) === "unreached" && next && next !== "unreached") {
        states.set(item.source, "delivered");
        changed = true;
      }
    }
  }
  /* Each card's lit ports, by handle id: a target port in its incoming edge's
     color, a source port in its outgoing flow color. */
  const ports = new Map<string, Record<string, string>>();
  const light = (nodeId: string, handle: string | null | undefined, color: string) => {
    if (!handle) return;
    const lit = ports.get(nodeId) ?? {};
    lit[handle] = color;
    ports.set(nodeId, lit);
  };
  const marked = edges.map((item): Edge => {
    const state = states.get(item.target) ?? "delivered";
    const look = edgeLooks[state];
    const still = look.animated && reducedMotion;
    const color = look.color ?? `var(--${item.class})`;
    light(item.source, item.sourceHandle, `var(--${item.class})`);
    light(item.target, item.targetHandle, color);
    return {
      ...item,
      animated: Boolean(look.animated) && !reducedMotion,
      style:
        [look.color && `stroke: ${color}`, still ? "stroke-dasharray: 3 3" : look.pattern]
          .filter(Boolean)
          .join("; ") || undefined
    };
  });
  return {
    nodes: nodes.map((item) =>
      ports.has(item.id) || item.data.ports
        ? { ...item, data: { ...item.data, ports: ports.get(item.id) } }
        : item
    ),
    edges: marked
  };
}

function scopedId(workflowId: string, localId: string): string {
  return `${workflowId}::${localId}`;
}

function toolRuntimeNodeId(toolId: ToolId): ToolRuntimeNodeId {
  return `tool:${toolId}`;
}

function approvalEvaluationNodeId(evaluationId: string): ApprovalEvaluationNodeId {
  return `approval:${evaluationId}`;
}

function isApprovalEvaluationNodeId(id: string): id is ApprovalEvaluationNodeId {
  return id.startsWith("approval:");
}

function isToolRuntimeNodeId(id: string): id is ToolRuntimeNodeId {
  return id.startsWith("tool:");
}

function sameFailedRetry(a: ToolRuntime | undefined, b: ToolRuntime | undefined): boolean {
  return Boolean(
    a &&
      b &&
      a.name === b.name &&
      a.status === "failed" &&
      b.status === "failed" &&
      a.detail === b.detail
  );
}

/**
 * Consecutive identical failures (same tool name + same error) become one card
 * with a count. Successful / distinct tools stay in order — not a focus-view fold.
 */
function collapseConsecutiveFailedRetries(
  order: RuntimeNodeId[],
  tools: Map<ToolId, ToolRuntime>
): Map<RuntimeNodeId, RuntimeNodeId> {
  const redirect = new Map<RuntimeNodeId, RuntimeNodeId>();
  const kept: RuntimeNodeId[] = [];
  for (const id of order) {
    const prev = kept.at(-1);
    if (
      prev &&
      isToolRuntimeNodeId(id) &&
      isToolRuntimeNodeId(prev) &&
      sameFailedRetry(tools.get(toolIdFromRuntimeNodeId(prev)), tools.get(toolIdFromRuntimeNodeId(id)))
    ) {
      const head = tools.get(toolIdFromRuntimeNodeId(prev))!;
      const next = tools.get(toolIdFromRuntimeNodeId(id))!;
      head.retryCount = (head.retryCount ?? 1) + 1;
      head.detail = next.detail;
      head.output = next.output;
      redirect.set(id, prev);
      continue;
    }
    kept.push(id);
  }
  order.splice(0, order.length, ...kept);
  return redirect;
}

function toolIdFromRuntimeNodeId(id: ToolRuntimeNodeId): ToolId {
  return id.slice("tool:".length);
}

/**
 * Focus view: a settled tool leaves the canvas, and nothing stands in for it —
 * the transcript holds what it did. Accumulated view is this not running.
 *
 * `linger` holds back the ones that just finished, so a call leaves having been
 * seen to finish rather than blinking out mid-turn. See AgentGraphOptions.linger.
 *
 * Same contract as the retry fold above: `order` is spliced in place.
 *
 * In-flight is read off the set the flow-group bookkeeping already maintains
 * rather than tested for on `status`. A status predicate has to enumerate
 * `requested`, `running`, `awaiting` and `approved` correctly to avoid hiding a
 * card, and hiding the one sitting at a human approval gate is the worst thing
 * this fold could do.
 */
function dropSettledTools(
  order: RuntimeNodeId[],
  inFlight: Set<ToolId>,
  linger: ReadonlySet<ToolId>
): void {
  const kept = order.filter((id) => {
    if (!isToolRuntimeNodeId(id)) return true;
    const toolId = toolIdFromRuntimeNodeId(id);
    return inFlight.has(toolId) || linger.has(toolId);
  });
  order.splice(0, order.length, ...kept);
}

function codeModeScriptFromToolInput(input: unknown): string | null {
  if (typeof input !== "object" || input == null || Array.isArray(input)) return null;
  const script = (input as Record<string, unknown>).script;
  return typeof script === "string" && script.trim() ? script : null;
}

function numericData(
  data: AgentNodeData,
  key: string,
  fallback: number
): number {
  const value = data[key];
  return typeof value === "number" ? value : fallback;
}

function dimensionsForData(data: AgentNodeData): NodeDimensions {
  const fallbackWidth = data.size === "large" ? largeStateNodeWidth : stateNodeWidth;
  const fallbackHeight = data.size === "large" ? largeStateNodeHeight : stateNodeHeight;
  return {
    width: numericData(data, "nodeWidth", fallbackWidth),
    height: numericData(data, "nodeHeight", fallbackHeight)
  };
}

/**
 * Where each row of a grid starts: under the tallest box in the row above it,
 * not under a nominal one.
 */
function rowTops(heights: readonly number[], columns: number, gap: number): number[] {
  const tops: number[] = [];
  let y = 0;
  for (let start = 0; start < heights.length; start += columns) {
    tops.push(y);
    y += Math.max(...heights.slice(start, start + columns)) + gap;
  }
  return tops;
}

/**
 * Where each column of a grid starts: past the widest box in the columns to its
 * left, not past a nominal one. One width per column across every row, so the
 * columns stay aligned and a wrap edge still drops straight down.
 */
function columnLefts(widths: readonly number[], columns: number, gap: number): number[] {
  const columnWidths: number[] = [];
  widths.forEach((width, index) => {
    const col = index % columns;
    columnWidths[col] = Math.max(columnWidths[col] ?? 0, width);
  });
  const lefts: number[] = [];
  let x = 0;
  for (const width of columnWidths) {
    lefts.push(x);
    x += width + gap;
  }
  return lefts;
}

function runtimeLayoutFor(
  order: RuntimeNodeId[],
  dataById: Map<RuntimeNodeId, AgentNodeData>
): RuntimeLayout {
  const attachReasoning = order.includes("model") && order.includes("reasoning");
  const flowOrder = attachReasoning
    ? order.filter((id) => id !== "reasoning")
    : order;
  const positions = new Map<RuntimeNodeId, { x: number; y: number }>();
  const sizes = flowOrder.map((id) => {
    const data = dataById.get(id);
    const dimensions = data
      ? dimensionsForData(data)
      : { width: stateNodeWidth, height: stateNodeHeight };
    if (!(attachReasoning && id === "model")) return dimensions;
    const reasoningData = dataById.get("reasoning");
    const reasoningDimensions = reasoningData
      ? dimensionsForData(reasoningData)
      : dimensions;
    return {
      width: Math.max(dimensions.width, reasoningDimensions.width),
      height: dimensions.height + modelReasoningGap + reasoningDimensions.height
    };
  });
  const tops = rowTops(
    sizes.map((size) => size.height),
    layout.columns,
    runtimeRowGap
  );
  const lefts = columnLefts(
    sizes.map((size) => size.width),
    layout.columns,
    runtimeColumnGap
  );
  let contentWidth = stateNodeWidth;
  let contentHeight = stateNodeHeight;

  for (let slot = 0; slot < flowOrder.length; slot += 1) {
    const dimensions = sizes[slot];
    const col = slot % layout.columns;
    const row = Math.floor(slot / layout.columns);
    const x = layout.gridStartX + lefts[col];
    const y = layout.gridStartY + tops[row];
    positions.set(flowOrder[slot], { x, y });
    contentWidth = Math.max(contentWidth, x - layout.gridStartX + dimensions.width);
    contentHeight = Math.max(contentHeight, y - layout.gridStartY + dimensions.height);
  }
  if (attachReasoning) {
    const modelPosition = positions.get("model");
    const modelData = dataById.get("model");
    if (modelPosition && modelData) {
      positions.set("reasoning", {
        x: modelPosition.x,
        y: modelPosition.y + dimensionsForData(modelData).height + modelReasoningGap
      });
    }
  }

  return {
    positions,
    boundaryWidth: layout.runtimePaddingX + contentWidth + layout.runtimePaddingX,
    boundaryHeight: layout.gridStartY - layout.runtime.y + contentHeight + 40
  };
}

function graphBounds(graph: AgentGraph): GraphBounds {
  if (graph.nodes.length === 0) {
    return { minX: 0, minY: 0, width: 0, height: 0 };
  }
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const item of graph.nodes) {
    const dimensions =
      item.type === "agentWorkflow"
        ? {
            width: numericData(item.data, "boundaryWidth", runtimeBoundaryWidth(1)),
            height: numericData(item.data, "boundaryHeight", runtimeBoundaryHeight(1))
          }
        : dimensionsForData(item.data);
    minX = Math.min(minX, item.position.x);
    minY = Math.min(minY, item.position.y);
    maxX = Math.max(maxX, item.position.x + dimensions.width);
    maxY = Math.max(maxY, item.position.y + dimensions.height);
  }
  return {
    minX,
    minY,
    width: maxX - minX,
    height: maxY - minY
  };
}

interface EmbeddedToolGrid {
  columns: number;
  columnLefts: number[];
  columnWidths: number[];
  rowTops: number[];
  width: number;
  height: number;
}

function embeddedToolGrid(bounds: readonly GraphBounds[], columns: number): EmbeddedToolGrid {
  const columnWidths: number[] = [];
  const rowHeights: number[] = [];
  bounds.forEach((item, index) => {
    const column = index % columns;
    const row = Math.floor(index / columns);
    columnWidths[column] = Math.max(columnWidths[column] ?? 0, item.width);
    rowHeights[row] = Math.max(rowHeights[row] ?? 0, item.height);
  });
  const offsets = (sizes: number[]) => {
    let next = 0;
    return sizes.map((size) => {
      const at = next;
      next += size + embeddedToolGap;
      return at;
    });
  };
  const sum = (sizes: number[]) => sizes.reduce((total, size) => total + size, 0) + (sizes.length - 1) * embeddedToolGap;
  return {
    columns,
    columnLefts: offsets(columnWidths),
    columnWidths,
    rowTops: offsets(rowHeights),
    width: sum(columnWidths),
    height: sum(rowHeights)
  };
}

/**
 * Children in reading order on a grid, each column as wide as its widest child. The column
 * count is whichever lets a 16:10 frame show the grid largest, so two children still stack
 * and sixty make a block the canvas can fit rather than a column it cannot. Tried for every
 * count, which is quadratic in the number of children; a few hundred is still nothing.
 *
 * A child that drew nothing stretches its header across its column instead of truncating
 * its name in a narrow card beside wide siblings.
 */
function layoutEmbeddedToolGraphs(graphs: AgentGraph[]): EmbeddedToolLayout | null {
  if (graphs.length === 0) return null;
  const bounds = graphs.map(graphBounds);
  const fitScale = (grid: EmbeddedToolGrid) => Math.min(16 / grid.width, 10 / grid.height);
  let grid = embeddedToolGrid(bounds, 1);
  for (let columns = 2; columns <= graphs.length; columns += 1) {
    const candidate = embeddedToolGrid(bounds, columns);
    if (fitScale(candidate) > fitScale(grid)) grid = candidate;
  }
  const placements = graphs.map((graph, index): EmbeddedToolPlacement => {
    const column = index % grid.columns;
    const row = Math.floor(index / grid.columns);
    return {
      graph:
        graph.nodes.length === 1
          ? {
              ...graph,
              nodes: graph.nodes.map((item) => ({
                ...item,
                data: { ...item.data, boundaryWidth: grid.columnWidths[column] }
              }))
            }
          : graph,
      x: embeddedToolPadding + grid.columnLefts[column]! - bounds[index]!.minX,
      y: embeddedToolHeaderHeight + grid.rowTops[row]! - bounds[index]!.minY
    };
  });
  return {
    dimensions: {
      width: Math.max(stateNodeWidth, grid.width + embeddedToolPadding * 2),
      height: embeddedToolHeaderHeight + grid.height + embeddedToolPadding
    },
    placements
  };
}

function codeModeContainerDimensions(childCount: number): NodeDimensions {
  const count = Math.max(1, childCount);
  const columns = Math.min(codeModeColumns, count);
  const rows = Math.ceil(count / columns);
  return {
    width:
      codeModePadding * 2 +
      columns * stateNodeWidth +
      Math.max(0, columns - 1) * codeModeColumnGap,
    height:
      codeModeHeaderHeight +
      rows * resultNodeHeight +
      Math.max(0, rows - 1) * codeModeRowGap +
      codeModePadding
  };
}

function codeModeChildPosition(index: number): { x: number; y: number } {
  const column = index % codeModeColumns;
  const row = Math.floor(index / codeModeColumns);
  return {
    x: codeModePadding + column * (stateNodeWidth + codeModeColumnGap),
    y: codeModeHeaderHeight + row * (resultNodeHeight + codeModeRowGap)
  };
}

/**
 * One graph paints as a ladder: boundary 0, its own flow edges 1, its cards 10,
 * a Code Mode host's children 14. A graph embedded in a card climbs that ladder
 * whole, nodes and edges alike.
 *
 * The Subagent activity container is a card at 10 with an opaque fill, so the
 * embedded graph has to start above it: lifted by 11, its boundary lands at 11,
 * its edges at 12 and its cards at 21. Lifted by less, the boundary — and with
 * it the subagent's name and state — painted under the container, and a child
 * with no cards of its own showed as an empty box.
 */
const nestedZIndexBoost = 11;

function offsetGraph(
  graph: AgentGraph,
  xOffset: number,
  yOffset: number,
  zIndexBoost = 0
): AgentGraph {
  return {
    ...graph,
    nodes: graph.nodes.map((item) => ({
      ...item,
      position: {
        x: item.position.x + xOffset,
        y: item.position.y + yOffset
      },
      zIndex: (item.zIndex ?? 0) + zIndexBoost
    })),
    edges: graph.edges.map((item) => ({
      ...item,
      zIndex: (item.zIndex ?? 0) + zIndexBoost
    }))
  };
}

function runtimeEdgeOptions(
  source: string,
  target: string,
  runtimeLayout: RuntimeLayout
): Parameters<typeof edge>[3] {
  const sourcePosition = runtimeLayout.positions.get(source as RuntimeNodeId);
  const targetPosition = runtimeLayout.positions.get(target as RuntimeNodeId);
  return sourcePosition && targetPosition && sourcePosition.y !== targetPosition.y
    ? { sourceHandle: "source-bottom", targetHandle: "target-top", kind: "main", wrap: true }
    : { kind: "main" };
}

function runtimeFlowSegments(
  order: RuntimeNodeId[],
  dataById: Map<RuntimeNodeId, AgentNodeData>
): Array<{ kind: "single"; ids: [RuntimeNodeId] } | { kind: "tools"; ids: RuntimeNodeId[] }> {
  const segments: Array<
    { kind: "single"; ids: [RuntimeNodeId] } | { kind: "tools"; ids: RuntimeNodeId[] }
  > = [];
  let index = 0;
  while (index < order.length) {
    const id = order[index];
    if (!isToolRuntimeNodeId(id)) {
      segments.push({ kind: "single", ids: [id] });
      index += 1;
      continue;
    }

    const flowGroup = dataById.get(id)?.flowGroup;
    if (typeof flowGroup !== "number") {
      segments.push({ kind: "single", ids: [id] });
      index += 1;
      continue;
    }

    const ids: RuntimeNodeId[] = [id];
    index += 1;
    while (
      index < order.length &&
      isToolRuntimeNodeId(order[index]) &&
      dataById.get(order[index])?.flowGroup === flowGroup
    ) {
      ids.push(order[index]);
      index += 1;
    }
    segments.push(ids.length > 1 ? { kind: "tools", ids } : { kind: "single", ids: [id] });
  }
  return segments;
}

function addRuntimeFlowEdges(
  edges: Edge[],
  order: RuntimeNodeId[],
  dataById: Map<RuntimeNodeId, AgentNodeData>,
  runtimeLayout: RuntimeLayout,
  inputSeen: boolean,
  inputPlacement: AgentGraphOptions["inputPlacement"]
): void {
  let sources: string[] = inputSeen && inputPlacement === "external" ? ["input"] : [];
  for (const segment of runtimeFlowSegments(order, dataById)) {
    if (sources.length > 0) {
      for (const source of sources) {
        for (const target of segment.ids) {
          edges.push(
            edge(
              `flow-${source}-${target}`,
              source,
              target,
              runtimeEdgeOptions(source, target, runtimeLayout)
            )
          );
        }
      }
    }
    sources = segment.ids;
  }
}

function terminalRuntimeSources(
  order: RuntimeNodeId[],
  dataById: Map<RuntimeNodeId, AgentNodeData>
): RuntimeNodeId[] {
  const segments = runtimeFlowSegments(order, dataById);
  return segments.at(-1)?.ids ?? [];
}

function textSection(
  label: string,
  text: string | undefined,
  kind: AgentNodeContext["kind"] = "text"
): AgentNodeContext | null {
  return typeof text === "string" && text.trim() ? { label, text, kind } : null;
}

/** Pretty-printed if it parses as JSON, kept verbatim if it does not. */
function maybeJsonSection(label: string, text: string | undefined): AgentNodeContext | null {
  if (typeof text !== "string" || !text.trim()) return null;
  try {
    return { label, text: JSON.stringify(JSON.parse(text), null, 2), kind: "json" };
  } catch {
    return { label, text, kind: "text" };
  }
}

const noLingeringTools: ReadonlySet<ToolId> = new Set();

/**
 * The tool this frame just settled, if it settled one.
 *
 * The same three cases that call markToolSettled inside the builder — a tool that
 * ended, one that failed, and an approval that was refused — and pointedly not a
 * granted approval, which tones the card "done" while the call has yet to run.
 *
 * Exported because the caller timing the linger has to answer exactly this
 * question, and two spellings of "has this finished" would drift apart. Hiding a
 * card that is still going, or holding one that is not, both start here.
 */
export function settledToolIdFromFrame(frame: AgentSseFrame): ToolId | null {
  if (!("type" in frame.data) || !("tool_id" in frame.data)) return null;
  if (frame.event === "tool_end" || frame.event === "tool_error") return frame.data.tool_id;
  if (frame.event === "tool_approval_resolved" && !frame.data.approved) {
    return frame.data.tool_id;
  }
  return null;
}

/**
 * How many steps of the cursor a finished tool stays drawn while a run is being
 * read back. One: the frame that finished it draws it done, and the next frame
 * dismisses it.
 *
 * One rather than a few, so the card answers for the event the reader is standing
 * on and nothing else. A tool held two steps is still on the canvas after the run
 * has moved on to the next thing, which puts a finished call next to a live one
 * and makes the reader work out which event they are looking at — the confusion
 * this whole fold exists to remove, reintroduced a step later.
 *
 * Since each settle is its own frame, this also means at most one card is held at
 * a time: what just finished, singular.
 *
 * At the live edge the beat is measured on the clock instead, because there the
 * run sets the pace and frames can arrive faster than they can be read.
 */
export const cursorHoldSteps = 1;

/**
 * The tool that settled on the frame the cursor is parked on, if one did.
 *
 * The read-back half of the hold. Scrubbing rebuilds the graph from the frames up
 * to the cursor, so a tool that finished one step ago is simply absent from focus
 * view — which is the same card blinking out unacknowledged that the hold exists
 * to stop, and it is what a reader stepping through a finished run sees for every
 * call in it.
 *
 * Pure and derived from the cursor alone, so it needs no timer and a given cursor
 * always draws the same graph: stepping back and forward again lands on what it
 * landed on before.
 */
export function settledNearCursor(
  timeline: readonly { frame: AgentSseFrame }[],
  steps: number = cursorHoldSteps
): ReadonlySet<ToolId> {
  const held = new Set<ToolId>();
  for (let at = Math.max(0, timeline.length - steps); at < timeline.length; at += 1) {
    const toolId = settledToolIdFromFrame(timeline[at].frame);
    if (toolId != null) held.add(toolId);
  }
  return held;
}

function valueSection(label: string, value: unknown): AgentNodeContext | null {
  if (value == null) return null;
  return { label, text: JSON.stringify(value, null, 2), kind: "json" };
}

/**
 * A token count, or a muted placeholder while the interaction that adds to it streams. Usage is
 * only reported when the interaction ends, so until then the settled figure is the finished
 * interactions' total, and a bare 0 would read as "this reply cost nothing".
 */
function tokenMetric(label: string, settled: number, streaming: boolean, unreportedCalls = 0): Metric {
  if (!streaming) {
    return unreportedCalls > 0
      ? { label, value: tokenFigure(settled, unreportedCalls), note: UNREPORTED_TIP }
      : { label, value: formatTokens(settled) };
  }
  return {
    label,
    value: settled > 0 ? `${formatTokens(settled)} + streaming\u2026` : "streaming\u2026",
    tone: "pending",
    note: "Usage is reported when the model interaction ends"
  };
}

export function buildAgentGraph(
  frames: AgentSseFrame[],
  options: AgentGraphOptions = {}
): AgentGraph {
  const inputPlacement = options.inputPlacement ?? "external";
  const showSubagentDispatch = options.showSubagentDispatch ?? true;
  const outputPlacement = options.outputPlacement ?? "external";
  const agentInterface = summarizeAgentInterface(options.agentInterface);
  const schemaOf = outputSchemaTracker();
  const lingeringTools = options.linger ?? noLingeringTools;
  let activeTurn: number | null = null;
  let status: AgentGraph["status"] = "idle";
  let failed = false;
  let currentUserMessage = "No message received";
  let queuedMessage = "";
  /* The envelope the rendered text came from — `{type, payload}` as the message was sent —
     so the inspector can show the raw message next to the readable one. */
  let currentUserEnvelope = "";
  let inputState = "waiting";
  let currentModel = "model idle";
  let modelState = "idle";
  let reasoningState = "idle";
  let reasoningDetail = "";
  /* When the model call that is doing the thinking opened, and when the last thought
     fragment landed — the two ends of "Thought for 4s". Both are frame timestamps the
     stream already stamps; nothing new is measured here. */
  let reasoningStartTs: number | null = null;
  let reasoningEndTs: number | null = null;
  let replyText = "";
  let replyIsData = false;
  let replyState = "waiting";
  let queued = 0;
  const queuedByTurn = new Map<number, [message: string, envelope: string]>();
  let runtimeHeaderPrefix: string | undefined;
  let subagentState = "idle";
  let subagentSubtitle = "";
  let subagentDetail = "";
  const tools = new Map<ToolId, ToolRuntime>();
  const codeModeChildren = new Map<ToolId, ToolId[]>();
  /* Keyed by evaluation_id, not tool_id: an agent may chain evaluators over one call. */
  const evaluations = new Map<string, EvaluationRuntime>();
  /* Gates that have been decided. An evaluation card is about a gate; once the gate is
     resolved the card has no subject left, and leaving it up next to a now-running tool
     reads as though approval were still outstanding. */
  const resolvedApprovalToolIds = new Set<ToolId>();
  /* Decided by REFUSAL specifically — the calls whose gate also ended the call. Tracked
     apart from the set above because the linger rule below may only ever apply to these;
     see the note there. */
  const deniedApprovalToolIds = new Set<ToolId>();
  /* Children are not in `runtimeNodeOrder` and so are not reached by the fold that
     drops settled tools. They get their own set, filled from the same call sites, so
     both halves of the graph answer "has this finished" the same way. */
  const settledChildToolIds = new Set<ToolId>();
  const activeCodeModeToolIds: ToolId[] = [];
  const activeRuntimeToolIds = new Set<ToolId>();
  let runtimeToolFlowGroup = 0;
  const runtimeNodeOrder: RuntimeNodeId[] = [];
  /* What the fold has put in runtimeNodeOrder: a turn of hundreds of tools made its
     `includes` the fold's hottest line. */
  const runtimeNodes = new Set<RuntimeNodeId>();
  let inputSeen = false;
  let outputSeen = false;
  let latestNodeId: LocalNodeId | null = null;
  /**
   * The last frame that touched each node, so no node can be reduced to an
   * apology. Every node in this graph exists BECAUSE a frame put it there, and
   * that frame is a better answer than a sentence saying there is nothing to
   * read — it is the ground truth the rest of the card is derived from.
   */
  const lastFrameByNode = new Map<LocalNodeId, AgentSseFrame["data"]>();
  let currentFrame: AgentSseFrame | null = null;

  function remember(id: LocalNodeId): void {
    if (currentFrame) lastFrameByNode.set(id, currentFrame.data);
  }

  function markInput(): void {
    inputSeen = true;
    if (inputPlacement === "runtime") {
      markRuntimeNode("input");
    } else {
      latestNodeId = "input";
      remember("input");
    }
  }

  function markRuntimeNode(id: RuntimeNodeId): void {
    if (!runtimeNodes.has(id)) {
      runtimeNodes.add(id);
      runtimeNodeOrder.push(id);
    }
    latestNodeId = id;
    remember(id);
  }

  function markOutput(): void {
    outputSeen = true;
    if (outputPlacement === "runtime") {
      markRuntimeNode("output");
    } else {
      latestNodeId = "output";
      remember("output");
    }
  }

  function markTool(toolId: ToolId, parentToolId?: ToolId): ToolRuntimeNodeId {
    const nodeId = toolRuntimeNodeId(toolId);
    if (parentToolId) {
      const childIds = codeModeChildren.get(parentToolId) ?? [];
      if (!childIds.includes(toolId)) {
        codeModeChildren.set(parentToolId, [...childIds, toolId]);
      }
      latestNodeId = nodeId;
      remember(nodeId);
      return nodeId;
    }
    markRuntimeNode(nodeId);
    return nodeId;
  }

  function flowGroupForTool(toolId: ToolId, parentToolId?: ToolId): number | undefined {
    if (parentToolId) return undefined;
    const existing = tools.get(toolId)?.flowGroup;
    if (typeof existing === "number") return existing;
    if (activeRuntimeToolIds.size === 0) runtimeToolFlowGroup += 1;
    activeRuntimeToolIds.add(toolId);
    return runtimeToolFlowGroup;
  }

  /* Called from tool_end, tool_error and a denied approval — and pointedly not from a
     granted one, which tones the card "done" while the call has yet to run. */
  function markToolSettled(toolId: ToolId, parentToolId?: ToolId): void {
    if (parentToolId) settledChildToolIds.add(toolId);
    else activeRuntimeToolIds.delete(toolId);
  }

  function toolRuntime(toolId: ToolId, name: string, parentToolId?: ToolId): ToolRuntime {
    const existing = tools.get(toolId);
    if (existing) {
      if (parentToolId && !existing.parentToolId) existing.parentToolId = parentToolId;
      if (!parentToolId && typeof existing.flowGroup !== "number") {
        existing.flowGroup = flowGroupForTool(toolId, parentToolId);
      }
      return existing;
    }
    return {
      id: toolId,
      name,
      status: "requested",
      tone: "tool",
      statusTone: "queue",
      subtitle: parentToolId ? "Code Mode host call" : "waiting to dispatch",
      parentToolId,
      flowGroup: flowGroupForTool(toolId, parentToolId)
    };
  }

  /* The one list of a host's visible children: both the container's size and the
     child nodes themselves read it, so a settled child cannot leave a hole the box
     is still sized around. */
  function codeModeHostChildIds(toolId: ToolId): ToolId[] {
    return (codeModeChildren.get(toolId) ?? []).filter(
      (childId) =>
        tools.has(childId) &&
        !(options.focus && settledChildToolIds.has(childId) && !lingeringTools.has(childId))
    );
  }

  function childToolParent(toolId: ToolId, isCodeModeTool: boolean): ToolId | undefined {
    if (isCodeModeTool) return undefined;
    // A call keeps the place its first frame gave it: one the model asked for alongside a
    // host starts while the host runs, but is not one of its script's calls.
    const existing = tools.get(toolId);
    return existing ? existing.parentToolId : activeCodeModeToolIds.at(-1);
  }

  function markCodeModeStarted(toolId: ToolId): void {
    if (!activeCodeModeToolIds.includes(toolId)) activeCodeModeToolIds.push(toolId);
  }

  function markCodeModeFinished(toolId: ToolId): void {
    const index = activeCodeModeToolIds.lastIndexOf(toolId);
    if (index !== -1) activeCodeModeToolIds.splice(index, 1);
  }

  function resetTurnTools(): void {
    tools.clear();
    evaluations.clear();
    resolvedApprovalToolIds.clear();
    deniedApprovalToolIds.clear();
    codeModeChildren.clear();
    settledChildToolIds.clear();
    activeCodeModeToolIds.splice(0, activeCodeModeToolIds.length);
    activeRuntimeToolIds.clear();
    runtimeToolFlowGroup = 0;
    for (let index = runtimeNodeOrder.length - 1; index >= 0; index -= 1) {
      if (isToolRuntimeNodeId(runtimeNodeOrder[index])) {
        runtimeNodes.delete(runtimeNodeOrder[index]);
        runtimeNodeOrder.splice(index, 1);
      }
    }
  }

  for (const frame of frames) {
    if (!("type" in frame.data)) continue;
    currentFrame = frame;
    const outputSchema = schemaOf(frame, options.agentInterface);
    if (frame.event === "message_accepted") {
      // The one event carrying what was sent, whatever it did to the turn. Only a QUEUED one
      // shows in the queue depth; an "opened" message is about to be the turn, and a "joined"
      // one is already running inside it.
      markInput();
      currentUserMessage = renderUserMessage(frame.data.handler, frame.data.payload);
      currentUserEnvelope = JSON.stringify({
        type: frame.data.handler,
        payload: frame.data.payload
      });
      if (frame.data.disposition === "queued") {
        queued += 1;
        queuedMessage = currentUserMessage;
        queuedByTurn.set(frame.data.turn_number, [currentUserMessage, currentUserEnvelope]);
        inputState = `${queued} queued`;
      } else {
        // The card now shows this message, and turn_started is at least a frame away (the
        // frame a turn jump lands on), so the label can't wait for it.
        inputState = `turn ${frame.data.turn_number}`;
      }
    }
    if (frame.event === "turn_started") {
      markInput();
      activeTurn = frame.data.turn_number;
      status = "running";
      failed = false;
      inputState = `turn ${frame.data.turn_number}`;
      // Later queued messages overwrote the card's text; the turn starting now is about its own.
      const opening = queuedByTurn.get(frame.data.turn_number);
      if (opening) [currentUserMessage, currentUserEnvelope] = opening;
      queuedByTurn.delete(frame.data.turn_number);
      modelState = "waiting";
      reasoningState = "waiting";
      reasoningDetail = "";
      reasoningStartTs = null;
      reasoningEndTs = null;
      replyText = "";
      replyIsData = false;
      replyState = "waiting";
      resetTurnTools();
      runtimeHeaderPrefix = undefined;
      if (queued > 0) queued -= 1;
    } else if (frame.event === "model_interaction_started") {
      markRuntimeNode("model");
      currentModel = frame.data.model ?? "unknown model";
      modelState = "running";
      reasoningState = "waiting";
      reasoningStartTs = frame.data.timestamp;
    } else if (frame.event === "model_interaction_ended") {
      markRuntimeNode("model");
      currentModel = frame.data.model ?? currentModel;
      modelState = "finished";
      if (reasoningState === "running" || frame.data.usage?.thought_tokens) {
        reasoningState = "captured";
      }
    } else if (frame.event === "thought_summary") {
      markRuntimeNode("reasoning");
      /* Accumulated, not assigned: every producer streams thought text in fragments, so
         the last frame holds the last few words rather than the thought. Assigning is
         what made a Gemini card look right — its mock summary arrives whole — while a
         real one showed its own tail. */
      reasoningDetail += thoughtDeltaText(frame.data.delta);
      reasoningState = "running";
      reasoningStartTs ??= frame.data.timestamp;
      reasoningEndTs = frame.data.timestamp;
    } else if (frame.event === "reply_delta") {
      markOutput();
      replyText += frame.data.text;
      replyIsData = streamingIsData(replyText, outputSchema);
      replyState = "streaming";
    } else if (frame.event === "text_annotation") {
      markOutput();
      replyState = "annotated";
    } else if (frame.event === "message_handler_end") {
      markOutput();
      status = "replied";
      const reply = handlerReply(frame.data, outputSchema);
      replyText = reply.text || replyText;
      replyIsData = reply.json !== undefined;
      /* "reply available" was wider than the chip, the same way "awaiting
         approval" was. The noun is carried by the card, which is titled Output. */
      replyState = "available";
    } else if (frame.event === "message_handler_error") {
      markOutput();
      status = "error";
      failed = true;
      replyText = frame.data.message;
      replyState = "error";
    } else if (frame.event === "turn_end") {
      activeTurn = frame.data.turn_number;
      status = "idle";
      modelState = "idle";
      /* A run that thought without summarizing has already been marked captured by the
         model_interaction_ended above, and downgrading it here would drop the note the
         card shows in place of the summary it never got. */
      reasoningState = reasoningDetail || reasoningState === "captured" ? "captured" : "idle";
      runtimeHeaderPrefix = "Turn end";
      latestNodeId = null;
    } else if (
      frame.event === "tool_requested" ||
      frame.event === "tool_start" ||
      frame.event === "tool_progress_delta" ||
      frame.event === "tool_end" ||
      frame.event === "tool_error"
    ) {
      const script =
        "tool_input" in frame.data
          ? codeModeScriptFromToolInput(frame.data.tool_input)
          : null;
      const isCodeModeTool = script != null || Boolean(tools.get(frame.data.tool_id)?.isCodeMode);
      const parentToolId = childToolParent(frame.data.tool_id, isCodeModeTool);
      markTool(frame.data.tool_id, parentToolId);
      const runtime = toolRuntime(frame.data.tool_id, frame.data.tool_name, parentToolId);
      runtime.name = frame.data.tool_name;
      runtime.parentToolId = parentToolId ?? runtime.parentToolId;
      if (script) {
        runtime.isCodeMode = true;
        runtime.script = script;
        runtime.subtitle = "Code Mode script";
      }
      if ("tool_input" in frame.data) {
        // tool_requested carries null when the model streamed arguments the backend could not
        // parse. Stringifying that puts the literal text "null" on the node, which reads as a
        // value the model sent rather than as one we lost.
        runtime.detail =
          frame.data.tool_input === null
            ? UNKNOWN_TOOL_INPUT
            : JSON.stringify(frame.data.tool_input, null, 2);
        runtime.input = runtime.detail;
      }
      if (frame.event === "tool_requested") {
        runtime.status = "requested";
        runtime.tone = "tool";
        runtime.statusTone = "queue";
        runtime.subtitle = runtime.isCodeMode
          ? "Code Mode script"
          : parentToolId
            ? "Code Mode host call"
            : modelState === "running"
              ? "model still running"
              : "waiting to dispatch";
      } else if (frame.event === "tool_start") {
        runtime.status = "running";
        runtime.tone = "tool";
        runtime.statusTone = "tool";
        runtime.subtitle = runtime.isCodeMode
          ? "Code Mode running"
          : parentToolId
            ? "host call running"
            : "execution started";
        if (runtime.isCodeMode) markCodeModeStarted(frame.data.tool_id);
      } else if (frame.event === "tool_progress_delta") {
        runtime.status = "running";
        runtime.tone = "tool";
        runtime.statusTone = "tool";
        runtime.subtitle = runtime.isCodeMode
          ? "Code Mode running"
          : parentToolId
            ? "host call running"
            : "execution in progress";
        runtime.detail = frame.data.progress_delta;
        runtime.output = frame.data.progress_delta;
      } else if (frame.event === "tool_end") {
        runtime.status = "done";
        runtime.tone = "done";
        runtime.statusTone = "done";
        runtime.subtitle = runtime.isCodeMode
          ? "Code Mode completed"
          : parentToolId
            ? "host call completed"
            : "execution completed";
        runtime.detail = frame.data.tool_output;
        runtime.output = frame.data.tool_output;
        if (runtime.isCodeMode) markCodeModeFinished(frame.data.tool_id);
        markToolSettled(frame.data.tool_id, parentToolId);
      } else if (frame.event === "tool_error") {
        runtime.status = "failed";
        runtime.tone = "error";
        runtime.statusTone = "error";
        runtime.subtitle = runtime.isCodeMode
          ? "Code Mode failed"
          : parentToolId
            ? "host call failed"
            : "execution failed";
        runtime.detail = frame.data.message;
        runtime.output = frame.data.message;
        if (runtime.isCodeMode) markCodeModeFinished(frame.data.tool_id);
        markToolSettled(frame.data.tool_id, parentToolId);
      }
      tools.set(frame.data.tool_id, runtime);
    } else if (
      frame.event === "auto_approval_evaluation_started" ||
      frame.event === "auto_approval_evaluation_ended" ||
      frame.event === "auto_approval_evaluation_superseded" ||
      frame.event === "auto_approval_evaluation_error"
    ) {
      /* Kept OUT of the tool's runtime on purpose. The evaluator is separate work running
         somewhere else; it gets its own node, wired to the gated call by one edge. */
      const evaluation: EvaluationRuntime = evaluations.get(frame.data.evaluation_id) ?? {
        id: frame.data.evaluation_id,
        toolId: frame.data.tool_id,
        toolName: frame.data.tool_name,
        evaluator: frame.data.evaluator,
        status: "evaluating",
        tone: "approval"
      };
      if (frame.event === "auto_approval_evaluation_ended") {
        const verdict = frame.data.verdict;
        evaluation.verdict = verdict;
        evaluation.status =
          verdict === "approve" ? "approved" : verdict === "deny" ? "denied" : "escalated";
        /* An escalate is not a failure — the evaluator correctly declined to decide — so
           it stays approval-toned, like the gate it hands back to. */
        evaluation.tone =
          verdict === "approve" ? "done" : verdict === "deny" ? "error" : "approval";
        evaluation.statusTone = evaluation.tone;
        evaluation.detail = frame.data.reason ?? undefined;
        evaluation.reasoning =
          Object.keys(frame.data.details).length > 0
            ? JSON.stringify(frame.data.details, null, 2)
            : undefined;
      } else if (frame.event === "auto_approval_evaluation_superseded") {
        /* Cancelled, because a human (or a policy cascade) settled the gate first. Drawn
           as a distinct outcome rather than as a failure: nothing went wrong, the answer
           simply arrived from elsewhere. */
        evaluation.status = "cancelled";
        evaluation.verdict = frame.data.verdict ?? undefined;
        evaluation.tone = "neutral";
        evaluation.statusTone = "neutral";
        evaluation.detail = frame.data.verdict
          ? `cancelled — had reached "${frame.data.verdict}"`
          : "cancelled — the gate was decided first";
      } else if (frame.event === "auto_approval_evaluation_error") {
        evaluation.status = "failed";
        evaluation.tone = "error";
        evaluation.statusTone = "error";
        evaluation.detail = frame.data.message;
      }
      evaluations.set(frame.data.evaluation_id, evaluation);
      /* Not markRuntimeNode: a satellite is positioned off its tool rather than taking a
         slot in the runtime grid. It is still the latest node, and still remembers the
         frame that put it there. */
      latestNodeId = approvalEvaluationNodeId(frame.data.evaluation_id);
      remember(latestNodeId);
    } else if (
      frame.event === "tool_approval_requested" ||
      frame.event === "tool_approval_resolved"
    ) {
      const script =
        "tool_input" in frame.data
          ? codeModeScriptFromToolInput(frame.data.tool_input)
          : null;
      const isCodeModeTool = script != null || Boolean(tools.get(frame.data.tool_id)?.isCodeMode);
      const parentToolId = childToolParent(frame.data.tool_id, isCodeModeTool);
      markTool(frame.data.tool_id, parentToolId);
      const runtime = toolRuntime(frame.data.tool_id, frame.data.tool_name, parentToolId);
      runtime.name = frame.data.tool_name;
      runtime.parentToolId = parentToolId ?? runtime.parentToolId;
      if (script) {
        runtime.isCodeMode = true;
        runtime.script = script;
      }
      if (frame.event === "tool_approval_requested") {
        /* Chip uppercases this on a 230px card; "awaiting approval" overflowed
           the header the same way "requested by model" did. */
        runtime.status = "awaiting";
        runtime.tone = "approval";
        runtime.statusTone = "approval";
        runtime.subtitle = runtime.isCodeMode
          ? "Code Mode approval gate"
          : parentToolId
            ? "host call approval gate"
            : "human approval gate";
        runtime.detail = JSON.stringify(frame.data.tool_input, null, 2);
        runtime.input = runtime.detail;
      } else {
        runtime.status = frame.data.approved ? "approved" : "denied";
        runtime.tone = frame.data.approved ? "done" : "error";
        runtime.statusTone = frame.data.approved ? "done" : "error";
        runtime.subtitle = frame.data.approved ? "approval granted" : "approval denied";
        runtime.detail = frame.data.reason ?? runtime.detail;
        resolvedApprovalToolIds.add(frame.data.tool_id);
        if (!frame.data.approved) {
          deniedApprovalToolIds.add(frame.data.tool_id);
          markToolSettled(frame.data.tool_id, parentToolId);
        }
      }
      tools.set(frame.data.tool_id, runtime);
    } else if (frame.event === "callback_requested" || frame.event === "callback_resolved") {
      /* Between tool_start and tool_end: the call is parked on a client, usually a person
         answering a question, so it waits the way an approval does until it is answered. */
      const parentToolId = childToolParent(frame.data.tool_id, false);
      markTool(frame.data.tool_id, parentToolId);
      const runtime = toolRuntime(frame.data.tool_id, frame.data.tool_name, parentToolId);
      if (frame.event === "callback_requested") {
        runtime.status = "awaiting";
        runtime.tone = "approval";
        runtime.statusTone = "approval";
        runtime.subtitle = "waiting on client";
        runtime.detail = JSON.stringify(frame.data.tool_input, null, 2);
        runtime.input = runtime.detail;
      } else {
        runtime.status = "running";
        runtime.tone = "tool";
        runtime.statusTone = "tool";
        runtime.subtitle = "callback answered";
      }
      tools.set(frame.data.tool_id, runtime);
    } else if (
      frame.event === "subagent_started" ||
      frame.event === "subagent_message_sent" ||
      frame.event === "subagent_reply_received" ||
      frame.event === "subagent_stopped" ||
      frame.event === "subagent_stream_unavailable"
    ) {
      if (!showSubagentDispatch) continue;
      markRuntimeNode("subagent");
      subagentSubtitle =
        "agent_key" in frame.data
          ? `${frame.data.agent_key} · ${frame.data.subagent_id}`
          : frame.data.subagent_id;
      subagentDetail = frame.data.workflow_id;
      if (frame.event === "subagent_started") {
        subagentState = "started";
      } else if (frame.event === "subagent_message_sent") {
        subagentState = `${frame.data.handler} → turn ${frame.data.subagent_turn}`;
      } else if (frame.event === "subagent_reply_received") {
        subagentState = `reply ${frame.data.outcome}`;
      } else if (frame.event === "subagent_stream_unavailable") {
        /* Short enough for the chip to render whole; see replyState above. */
        subagentState = "unavailable";
      } else {
        subagentState = "stopped";
      }
    }
  }

  const retryRedirect = collapseConsecutiveFailedRetries(runtimeNodeOrder, tools);
  if (latestNodeId && retryRedirect.has(latestNodeId)) {
    latestNodeId = retryRedirect.get(latestNodeId)!;
  }

  /* Read before the focus fold, which can drop the reasoning node: the summary
     below keys "drew a card and said nothing" off the card having existed, and
     asking runtimeNodeOrder after the splice answers about the wrong thing. */
  const reasoningSeen = runtimeNodeOrder.includes("reasoning");
  if (options.focus) {
    dropSettledTools(runtimeNodeOrder, activeRuntimeToolIds, lingeringTools);
    /* A finished thought trace is a completed event too, and the transcript
       holds it either way. While fragments are still arriving it stays. */
    if (reasoningState !== "running") {
      const at = runtimeNodeOrder.indexOf("reasoning");
      if (at >= 0) runtimeNodeOrder.splice(at, 1);
    }
    /* Not optional bookkeeping: `active: latestNodeId === id` is what marks the
       live card, so a turn whose last tool just settled off the canvas would
       have no active node at all. The newest survivor is where the run is. */
    if (latestNodeId && !runtimeNodeOrder.includes(latestNodeId)) {
      latestNodeId = runtimeNodeOrder.at(-1) ?? null;
    }
  }

  const embeddedToolLayout = layoutEmbeddedToolGraphs(options.embeddedToolGraphs ?? []);
  if (embeddedToolLayout && !runtimeNodeOrder.includes("tool-container")) {
    runtimeNodeOrder.push("tool-container");
  }
  const usage = summarizeCost(frames);

  /**
   * What the thought card says when the thinking produced no text.
   *
   * Keyed on the card existing at all, which is the only state that can render blank: this
   * node is drawn because a thought_summary frame arrived, so a frame arrived and carried
   * nothing readable — a Pydantic AI signature-only delta, or a shape newer than the
   * extractor. A run that asks for no summary (`Reasoning(summary=None)`, which is what
   * examples/sandbox_tools/coding_agent runs on) streams no such frame and draws no card,
   * so it is already distinguishable; what was not distinguishable is this.
   */
  const reasoningSummary = reasoningDetail || (reasoningSeen ? NO_THOUGHT_SUMMARY : "");
  /* Only once the thinking is over: while fragments are still arriving the end moves, and a
     number that climbs while you read it is not a duration. */
  const reasoningSeconds =
    reasoningState === "captured" && reasoningStartTs != null && reasoningEndTs != null
      ? Math.max(0, reasoningEndTs - reasoningStartTs)
      : null;

  /**
   * What the inspector shows, per node kind. Every branch here ends with the
   * frame that last touched the node, so the list is never empty: a node exists
   * because a frame put it there, and printing that frame beats printing a
   * sentence about having nothing to print.
   *
   * The two synthetic nodes — the tool container and the runtime boundary — have
   * no frame of their own, so they describe what they are made of instead.
   */
  function replySection(label: string): AgentNodeContext | null {
    return streamingIsData(replyText) ? maybeJsonSection(label, stripJsonFence(replyText)) : textSection(label, replyText, "markdown");
  }

  function contextFor(id: LocalNodeId): AgentNodeContext[] {
    const sections: Array<AgentNodeContext | null> = [];

    if (id === "input") {
      const rendered = currentUserMessage || queuedMessage;
      sections.push(textSection("User message", rendered, "markdown"));
      if (currentUserEnvelope && currentUserEnvelope !== rendered) {
        sections.push(maybeJsonSection("Raw message", currentUserEnvelope));
      }
    } else if (id === "model") {
      /* The model's own output and its thinking — the two things asked for by
         name. Both were already in this scope; nothing reached them because the
         inspector only ever read `detail`, and this node has never had one. */
      sections.push(
        replySection("Model output"),
        textSection("Thought summary", reasoningSummary, "markdown"),
        textSection("Model", currentModel)
      );
    } else if (id === "reasoning") {
      sections.push(textSection("Thought summary", reasoningSummary, "markdown"));
    } else if (id === "output") {
      sections.push(replySection("Reply"));
    } else if (id === "subagent") {
      sections.push(
        textSection("Subagent", subagentSubtitle),
        textSection("Workflow", subagentDetail)
      );
    } else if (isToolRuntimeNodeId(id)) {
      const runtime = tools.get(toolIdFromRuntimeNodeId(id));
      sections.push(
        runtime?.script ? { label: "Script", text: runtime.script, kind: "code" } : null,
        maybeJsonSection("Tool input", runtime?.input),
        maybeJsonSection("Tool output", runtime?.output)
      );
    } else if (isApprovalEvaluationNodeId(id)) {
      /* The EVALUATION's own content. Before this node existed the events were folded into
         the tool card, so opening the approval showed the tool's input and output — the
         one thing it is not about. */
      const evaluation = evaluations.get(id.slice("approval:".length));
      sections.push(
        textSection("Evaluator", evaluation?.evaluator),
        textSection("Verdict", evaluation?.verdict ?? evaluation?.status),
        textSection("Reason", evaluation?.detail, "markdown"),
        evaluation?.reasoning
          ? { label: "Evaluator reasoning", text: evaluation.reasoning, kind: "json" }
          : null,
        textSection("Gated call", evaluation?.toolName)
      );
    } else if (id === "tool-container") {
      sections.push(
        valueSection(
          "Delegated runtimes",
          (options.embeddedToolGraphs ?? []).map((graph) => ({
            status: graph.status,
            activeTurn: graph.activeTurn,
            nodes: graph.nodes.length
          }))
        )
      );
    }

    sections.push(valueSection("Event payload", lastFrameByNode.get(id)));
    const built = sections.filter((section): section is AgentNodeContext => section != null);
    /* Unreachable for every id above, and the reason there is no apology string
       left in this file: a node with nothing else still describes itself. */
    return built.length > 0 ? built : [{ label: "Node", text: id, kind: "text" }];
  }

  function nodeDataFor(id: LocalNodeId): AgentNodeData {
    return { ...baseNodeDataFor(id), context: contextFor(id) };
  }

  function baseNodeDataFor(id: LocalNodeId): AgentNodeData {
    if (id === "input") {
      return {
        tone: status === "running" ? "queue" : "neutral",
        title: "Input",
        state: inputState,
        subtitle: "user message",
        /* The text, not the envelope it arrived in. The card used to show the
           raw `{"type":"ask","payload":{...}}` — barely legible in four lines
           and plainly wrong now the Result region is big enough to read. The
           envelope is still in the inspector, as "Raw message". */
        detail: currentUserMessage || queuedMessage,
        nodeHeight: resultNodeHeight,
        active: latestNodeId === id
      };
    }
    if (id === "model") {
      return {
        tone: modelState === "running" ? "model" : "neutral",
        title: "Model interaction",
        state: modelState,
        subtitle: currentModel,
        active: latestNodeId === id,
        size: "large",
        metrics: [
          tokenMetric("input", usage.tokens.input, modelState === "running", usage.unreportedCalls),
          tokenMetric("output", usage.tokens.output, modelState === "running", usage.unreportedCalls)
        ]
      };
    }
    if (id === "reasoning") {
      return {
        tone:
          reasoningState === "running" || reasoningState === "captured"
            ? "reasoning"
            : "neutral",
        title: "Thought summary",
        state: reasoningState,
        /* How long it thought, once that is a settled number, because that is the question
           a finished thought card is asked; the token count is what it answers until then. */
        subtitle:
          reasoningSeconds != null && reasoningSeconds >= 1
            ? `Thought for ${formatDuration(reasoningSeconds)}`
            : usage.tokens.thought > 0
              ? `${formatTokens(usage.tokens.thought)} thought tokens`
              : "thinking trace",
        detail: reasoningSummary,
        nodeHeight: resultNodeHeight,
        active: latestNodeId === id
      };
    }
    if (isToolRuntimeNodeId(id)) {
      const runtime = tools.get(toolIdFromRuntimeNodeId(id));
      /* Always a string: the Result region is a slot the node reserves, not
         something that appears once the tool has said anything. */
      const detail = runtime?.detail ?? "";
      const childCount = runtime?.id ? codeModeHostChildIds(runtime.id).length : 0;
      const codeModeDimensions =
        runtime?.isCodeMode && childCount > 0
          ? codeModeContainerDimensions(childCount)
          : null;
      const toolStatus = runtime?.status ?? "idle";
      const retryCount = runtime?.retryCount ?? 1;
      return {
        tone: runtime?.tone ?? "neutral",
        title: runtime?.name ?? "Tool",
        state: retryCount > 1 ? `${toolStatus} ×${retryCount}` : toolStatus,
        statusTone: runtime?.statusTone,
        subtitle: runtime?.subtitle ?? "tool lifecycle",
        detail,
        size: codeModeDimensions ? "container" : undefined,
        nodeWidth: codeModeDimensions?.width,
        nodeHeight: codeModeDimensions?.height ?? resultNodeHeight,
        /* Only for a Code Mode host: the container class otherwise squeezes the
           result body to leave room for children, which is right for the subagent
           container and wrong for a card showing a script. */
        resultHeight: codeModeDimensions ? codeModeScriptHeight : undefined,
        active: latestNodeId === id,
        toolId: runtime?.id,
        codeMode: runtime?.isCodeMode,
        flowGroup: runtime?.flowGroup
      };
    }
    if (isApprovalEvaluationNodeId(id)) {
      const evaluation = evaluations.get(id.slice("approval:".length));
      return {
        tone: evaluation?.tone ?? "approval",
        /* The evaluator, not the tool: this card is about who judged, and the tool it
           judged is one edge away. */
        title: evaluation?.evaluator ?? "approval evaluator",
        state: evaluation?.status ?? "evaluating",
        statusTone: evaluation?.statusTone,
        subtitle: evaluation ? `auto approval · ${evaluation.toolName}` : "auto approval",
        detail: evaluation?.detail ?? "",
        nodeHeight: stateNodeHeight,
        active: latestNodeId === id
      };
    }
    if (id === "tool-container") {
      return {
        ...delegatedState(options.embeddedToolGraphs ?? []),
        title: "Subagent activity",
        subtitle: "delegated runtimes",
        active: latestNodeId === id,
        size: "container",
        nodeWidth: embeddedToolLayout?.dimensions.width ?? stateNodeWidth,
        nodeHeight: embeddedToolLayout?.dimensions.height ?? stateNodeHeight
      };
    }
    if (id === "subagent") {
      return {
        tone:
          subagentState === "unavailable"
            ? "error"
            : subagentState === "stopped"
              ? "done"
              : "agent",
        title: "Subagent",
        state: subagentState,
        subtitle: subagentSubtitle,
        detail: subagentDetail,
        nodeHeight: resultNodeHeight,
        active: latestNodeId === id
      };
    }
    if (id === "output") {
      return {
        tone: status === "error" ? "error" : replyText ? "done" : "neutral",
        title: "Output",
        state: replyState,
        subtitle: "streaming response",
        detail: replyText,
        markdown: !replyIsData && replyState !== "error",
        nodeHeight: resultNodeHeight,
        active: latestNodeId === id
      };
    }
    return {
      tone: "neutral",
      title: "Unknown",
      state: "unknown"
    };
  }

  const runtimeDataById = new Map<RuntimeNodeId, AgentNodeData>(
    runtimeNodeOrder.map((id) => [id, nodeDataFor(id)])
  );
  const runtimeLayout = runtimeLayoutFor(runtimeNodeOrder, runtimeDataById);
  const boundaryWidth = runtimeLayout.boundaryWidth;
  const boundaryHeight = runtimeLayout.boundaryHeight;

  const runtimeNode = node(
    "agent-runtime",
    layout.runtime,
    {
      tone: status === "error" ? "error" : "agent",
      title: "Agent runtime",
      state: status === "idle" ? "idle" : status,
      subtitle: "model calls, tool execution, and internal state",
      headerPrefix: runtimeHeaderPrefix,
      boundaryWidth,
      boundaryHeight,
      interfaces: agentInterface,
      /* The boundary is drawn from state rather than from any one frame, so it
         says what it contains: the run's status and the functions the agent
         exposes, of which the card itself only has room for two. */
      context: [
        {
          label: "Runtime",
          text: JSON.stringify(
            {
              status,
              activeTurn,
              model: currentModel,
              nodes: runtimeNodeOrder,
              tokens: usage.tokens
            },
            null,
            2
          ),
          kind: "json"
        },
        ...(agentInterface.length > 0
          ? [
              {
                label: "Agent interface",
                text: JSON.stringify(agentInterface, null, 2),
                kind: "json" as const
              }
            ]
          : [])
      ]
    },
    "agentWorkflow"
  );
  const nodes: Node<AgentNodeData>[] = [runtimeNode];
  if (inputSeen && inputPlacement === "external") {
    nodes.push(node("input", layout.input, nodeDataFor("input")));
  }
  nodes.push(
    ...runtimeNodeOrder.map((id) =>
      node(
        id,
        runtimeLayout.positions.get(id) ?? { x: layout.gridStartX, y: layout.gridStartY },
        runtimeDataById.get(id) ?? nodeDataFor(id)
      )
    )
  );
  /* Which tool cards actually made it onto the canvas this frame. An approval satellite
     is drawn from its call, so a satellite whose call was folded away (focus view, retry
     stacking) would leave an edge pointing at nothing. */
  const drawnToolNodeIds = new Set<string>(
    runtimeNodeOrder.filter((id) => isToolRuntimeNodeId(id))
  );
  for (const parentToolId of codeModeChildren.keys()) {
    const parentNodeId = toolRuntimeNodeId(parentToolId);
    const parentPosition = runtimeLayout.positions.get(parentNodeId);
    if (!parentPosition) continue;
    /* Survivors repack from the first slot, so the grid has no gaps where the
       finished calls were. */
    for (const [index, childToolId] of codeModeHostChildIds(parentToolId).entries()) {
      const childNodeId = toolRuntimeNodeId(childToolId);
      const childOffset = codeModeChildPosition(index);
      nodes.push({
        ...node(
          childNodeId,
          {
            x: parentPosition.x + childOffset.x,
            y: parentPosition.y + childOffset.y
          },
          nodeDataFor(childNodeId)
        ),
        zIndex: 14
      });
      drawnToolNodeIds.add(childNodeId);
    }
  }
  if (outputSeen && outputPlacement === "external") {
    nodes.push(node("output", outputPosition(boundaryWidth), nodeDataFor("output")));
  }

  const edges: Edge[] = [];
  /**
   * Where a gated call's approval satellites hang: under the card that OWNS the call.
   *
   * For an ordinary tool that is its own card. For a Code Mode host call it is the HOST's
   * container, not the child cell inside it — children are packed in a grid, so a
   * satellite dropped below a child would land on the next child. Hanging it under the
   * container puts it in empty space, and the edge still runs from the child, so which
   * host call is being approved stays legible.
   */
  function approvalAnchorOwner(toolId: ToolId): ToolRuntimeNodeId {
    const parentToolId = tools.get(toolId)?.parentToolId;
    return toolRuntimeNodeId(parentToolId ?? toolId);
  }

  /* One column per owner, so several gated calls under one Code Mode host do not stack. */
  const satellitesPerOwner = new Map<ToolRuntimeNodeId, number>();

  /* Each auto approval evaluation as its own card, hung off the gated call by a single
     edge — the evaluator runs outside the tool, and the graph says so. */
  for (const evaluation of evaluations.values()) {
    /* Dismissed once the gate is decided. The card exists to say what is happening to an
       OPEN approval; after the decision the tool just runs, and a leftover "escalated"
       card beside it claims the call is still waiting on someone. The transcript, the
       replay log and the waterfall keep the record.

       TWO EXCEPTIONS, both about not deleting a card before it has been read:

       - A CANCELLED evaluation is kept. It is the one terminal the resolution CAUSES
         rather than merely follows, so dismissing it on that same resolution would mean
         an evaluator pre-empted by a human could never be seen to have been pre-empted —
         and being able to see exactly that is why the cancellation is published at all.
         It leaves with its tool, at the end of the turn or when focus view drops the call.
       - A call the decision SETTLED — a DENIAL — lingers a beat so it is seen to finish.
         Its evaluation lingers with it rather than vanishing out from under the card it
         is attached to.

       That second exception is gated on the call having been DENIED, not merely on it
       being in `lingeringTools`. The linger set is "anything that settled in the last
       beat", which includes a `tool_end` — so keying off it alone meant an approved call
       put its own tool id back in the set when it FINISHED, and the evaluation card that
       had been dismissed at the approval reappeared minutes later as the tool wrapped up.
       Only a refusal can resurrect a card, and only for the beat its own call is held. */
    const gateDecided = resolvedApprovalToolIds.has(evaluation.toolId);
    const heldWithRefusedCall =
      deniedApprovalToolIds.has(evaluation.toolId) &&
      lingeringTools.has(evaluation.toolId);
    if (gateDecided && evaluation.status !== "cancelled" && !heldWithRefusedCall) {
      continue;
    }
    const toolNodeId = toolRuntimeNodeId(evaluation.toolId);
    // The call's card can be gone (focus view drops settled ones); its satellite goes too,
    // or the edge below would point at a node that is not drawn.
    if (!drawnToolNodeIds.has(toolNodeId)) continue;
    const ownerNodeId = approvalAnchorOwner(evaluation.toolId);
    const ownerPosition = runtimeLayout.positions.get(ownerNodeId);
    if (!ownerPosition) continue;
    /* Read off the owner's own card rather than assumed: a Code Mode host is a container
       sized to its children, so the drop has to clear whatever it actually grew to. */
    const ownerHeight = numericData(
      runtimeDataById.get(ownerNodeId) ?? nodeDataFor(ownerNodeId),
      "nodeHeight",
      resultNodeHeight
    );
    const column = satellitesPerOwner.get(ownerNodeId) ?? 0;
    satellitesPerOwner.set(ownerNodeId, column + 1);
    const nodeId = approvalEvaluationNodeId(evaluation.id);
    nodes.push({
      ...node(
        nodeId,
        {
          x: ownerPosition.x + column * (stateNodeWidth + approvalEvaluationGapX),
          y: ownerPosition.y + ownerHeight + approvalEvaluationGapY
        },
        nodeDataFor(nodeId)
      ),
      zIndex: 14
    });
    edges.push(
      edge(nodeId, toolNodeId, nodeId, {
        sourceHandle: "source-bottom",
        targetHandle: "target-top",
        kind: "approval"
      })
    );
  }
  const toolPosition = runtimeLayout.positions.get("tool-container");
  if (toolPosition && embeddedToolLayout) {
    for (const placement of embeddedToolLayout.placements) {
      const embeddedGraph = offsetGraph(
        placement.graph,
        toolPosition.x + placement.x,
        toolPosition.y + placement.y,
        nestedZIndexBoost
      );
      nodes.push(...embeddedGraph.nodes);
      edges.push(...embeddedGraph.edges);
    }
  }
  const reasoningAttachedToModel =
    runtimeNodeOrder.includes("model") && runtimeNodeOrder.includes("reasoning");
  const runtimeFlowOrder = reasoningAttachedToModel
    ? runtimeNodeOrder.filter((id) => id !== "reasoning")
    : runtimeNodeOrder;
  if (reasoningAttachedToModel) {
    edges.push(
      edge(
        "flow-model-reasoning",
        "model",
        "reasoning",
        {
          sourceHandle: "source-bottom",
          targetHandle: "target-top",
          kind: "reasoning"
        }
      )
    );
  }
  addRuntimeFlowEdges(
    edges,
    runtimeFlowOrder,
    runtimeDataById,
    runtimeLayout,
    inputSeen,
    inputPlacement
  );
  if (outputSeen && outputPlacement === "external") {
    const outputSources = terminalRuntimeSources(runtimeFlowOrder, runtimeDataById);
    if (outputSources.length > 0) {
      edges.push(
        ...outputSources.map((source) =>
          edge(
            `flow-${source}-output`,
            source,
            "output",
            { kind: "output" }
          )
        )
      );
    } else if (inputSeen) {
      edges.push(
        edge("flow-input-output", "input", "output", {
          kind: "output"
        })
      );
    }
  }

  return {
    ...markEdgeCues(nodes, edges, options.reducedMotion ?? false),
    activeTurn,
    status,
    failed,
    usage
  };
}

function graphSourceLabel(agent: AgentGraphSource): string {
  if (agent.role === "parent") return "Parent agent";
  return `${agent.agentKey ?? "subagent"} subagent`;
}

/* The parent's label is the agent name the console header already shows, so
   repeating it here only pushes the real description off the card. */
function graphSourceSubtitle(agent: AgentGraphSource): string {
  const bits = [agent.role === "parent" ? "" : agent.label];
  if (agent.subagentId) bits.unshift(`subagent ${agent.subagentId}`);
  if (agent.stopped) bits.push("stopped");
  return bits.filter(Boolean).join(" · ");
}

/** What the parent's frames, and the merge's marker, say about one child. */
interface DelegationRecord {
  unavailable: boolean;
  reason: string;
  reasonCode?: SubagentUnavailableCode;
  reply?: "ok" | "error";
  stoppedByParent: boolean;
}

function scopedGraph(
  agent: AgentGraphSource,
  graph: AgentGraph,
  xOffset: number,
  yOffset: number,
  delegation?: DelegationRecord
): AgentGraph {
  const scopedNodeId = (id: string) =>
    id.includes("::") ? id : scopedId(agent.workflowId, id);
  /**
   * A boundary with nothing inside it is its header, and nothing else.
   *
   * runtimeLayoutFor seeds contentWidth/contentHeight with one nominal card
   * before its loop runs, so a runtime that contributed NO nodes still reserved
   * room for one: 290px around an 88px header, which is the 202px of nothing
   * under a stopped subagent whose own stream was never readable.
   *
   * Running or not: a child announced and never started kept an empty box under
   * its header for as long as it waited. Its first card grows the boundary when
   * it arrives, which a scrub across that frame shows as a step.
   */
  const emptyBoundary = agent.role === "subagent" && graph.nodes.length === 1;
  /* Whether or not the child drew anything before the merge gave up: a partly streamed
     child's own frames stop mid-turn, and would say "running" forever. */
  const unreadable =
    delegation?.unavailable === true
      ? unreadableChildState({
          reply: delegation.reply,
          stoppedByParent: delegation.stoppedByParent,
          executionStatus: agent.executionStatus,
          closed: agent.stopped === true
        })
      : undefined;
  const reason =
    unreadable && delegation
      ? unreadableReasonLine(delegation.reasonCode, delegation.reason)
      : undefined;
  /* The reason line grows the header, so what the boundary holds moves down by as much. */
  const reasonShift = reason ? runtimeReasonHeight : 0;
  const nodes = graph.nodes.map((item): Node<AgentNodeData> => {
    const position = {
      x: item.position.x + xOffset,
      y: item.position.y + yOffset + (item.id === "agent-runtime" ? 0 : reasonShift)
    };
    if (item.id !== "agent-runtime") return { ...item, id: scopedNodeId(item.id), position };
    const data: AgentNodeData = {
      ...item.data,
      ...(unreadable && delegation
        ? {
            tone: unreadableLooks[unreadable],
            state: unreadable,
            reason,
            context: [
              unreadableContext(delegation, agent.executionStatus),
              ...(item.data.context ?? [])
            ]
          }
        : {
            tone: agent.stopped ? "done" : item.data.tone,
            state: agent.stopped ? "stopped" : item.data.state
          }),
      runtimeRole: agent.role,
      boundaryHeight:
        (emptyBoundary ? runtimeHeaderHeight : numericData(item.data, "boundaryHeight", runtimeHeaderHeight)) +
        reasonShift,
      title: `${graphSourceLabel(agent)} runtime`,
      /* The runtime's own "model calls, tool execution…" describes cards an empty one lacks. */
      subtitle: [graphSourceSubtitle(agent), emptyBoundary ? "" : item.data.subtitle]
        .filter(Boolean)
        .join(" · ")
    };
    /* Rebuilt from the rewritten title and state, or every child announces itself as the
       generic "Agent runtime, idle" buildAgentGraph named it. */
    const ariaLabel = [data.title, data.state, reason].filter(Boolean).join(", ");
    return { ...item, id: scopedNodeId(item.id), position, data, ariaLabel };
  });
  const edges = graph.edges.map((item) => ({
    ...item,
    id: item.id.includes("::") ? item.id : scopedId(agent.workflowId, item.id),
    source: scopedNodeId(item.source),
    target: scopedNodeId(item.target)
  }));

  return { ...graph, nodes, edges, unreadable };
}

function graphWidth(graph: AgentGraph): number {
  return graphBounds(graph).width;
}

/**
 * The Subagent activity container's state, from its children's: a failed child makes it an
 * error, one still running (or dispatched and not yet started) keeps it delegating, and once
 * every child has finished it is complete. A child whose stream could not be read counts by
 * its unreadableChildState instead of its own frames, which may have stopped mid-turn: only
 * "delegating" keeps the container busy, and only "error" fails it.
 */
function delegatedState(children: readonly AgentGraph[]): Pick<AgentNodeData, "tone" | "state"> {
  if (children.some((child) => child.failed || child.unreadable === "error")) {
    return { tone: "error", state: "error" };
  }
  const busy = children.some((child) => {
    if (child.unreadable !== undefined) return child.unreadable === "delegating";
    switch (child.status) {
      case "running":
        return true;
      case "idle":
        return child.activeTurn === null;
      case "replied":
      case "error":
        return false;
      default: {
        const unhandled: never = child.status;
        throw new Error(`No container state for child status ${String(unhandled)}`);
      }
    }
  });
  return busy ? { tone: "tool", state: "delegating" } : { tone: "done", state: "complete" };
}

function composeStatus(statuses: AgentGraph["status"][]): AgentGraph["status"] {
  if (statuses.includes("error")) return "error";
  if (statuses.includes("running")) return "running";
  if (statuses.includes("replied")) return "replied";
  return "idle";
}

export function buildAgentTreeGraph(
  agents: AgentGraphSource[],
  /* Forwarded to every agent in the tree, not just the root: a focus mode that
     only applied to the parent would leave a busy subagent drawing every card. */
  options: Pick<AgentGraphOptions, "focus" | "linger" | "reducedMotion"> = {}
): AgentGraph {
  if (agents.length === 0) return buildAgentGraph([], options);

  const agentByWorkflow = new Map(agents.map((agent) => [agent.workflowId, agent]));
  const childrenByParent = new Map<string, AgentGraphSource[]>();
  for (const agent of agents) {
    if (!agent.parentWorkflowId) continue;
    const children = childrenByParent.get(agent.parentWorkflowId) ?? [];
    children.push(agent);
    childrenByParent.set(agent.parentWorkflowId, children);
  }
  /* Read here rather than through showSubagentDispatch, which would also draw
     the dispatch card the tree already shows as the child's own boundary. The
     marker rides the child's frames and the rest the parent's, so only each
     list's own order is relied on: a new message clears the last reply. */
  const delegations = new Map<string, DelegationRecord>();
  const delegationOf = (workflowId: string): DelegationRecord => {
    const found = delegations.get(workflowId);
    if (found) return found;
    const created: DelegationRecord = { unavailable: false, reason: "", stoppedByParent: false };
    delegations.set(workflowId, created);
    return created;
  };
  for (const frame of agents.flatMap((agent) => agent.frames)) {
    if (frame.event === "subagent_message_sent") {
      delegationOf(frame.data.workflow_id).reply = undefined;
    } else if (frame.event === "subagent_reply_received") {
      delegationOf(frame.data.workflow_id).reply = frame.data.outcome;
    } else if (frame.event === "subagent_stopped") {
      delegationOf(frame.data.workflow_id).stoppedByParent = true;
    } else if (frame.event === "subagent_stream_unavailable") {
      const record = delegationOf(frame.data.workflow_id);
      record.unavailable = true;
      record.reason = frame.data.reason;
      record.reasonCode = frame.data.reason_code;
    }
  }

  const builtGraphs: AgentGraph[] = [];
  function buildNestedAgentGraph(
    agent: AgentGraphSource,
    seen = new Set<string>()
  ): AgentGraph {
    const nextSeen = new Set(seen);
    nextSeen.add(agent.workflowId);
    const childGraphs = (childrenByParent.get(agent.workflowId) ?? [])
      .filter((child) => !nextSeen.has(child.workflowId))
      .map((child) => buildNestedAgentGraph(child, nextSeen));
    const graph = buildAgentGraph(agent.frames, {
      inputPlacement: agent.role === "subagent" ? "runtime" : "external",
      showSubagentDispatch: false,
      outputPlacement: agent.role === "subagent" ? "runtime" : "external",
      agentInterface: agent.agentInterface,
      embeddedToolGraphs: childGraphs,
      focus: options.focus,
      linger: options.linger,
      reducedMotion: options.reducedMotion
    });
    const scoped = scopedGraph(agent, graph, 0, 0, delegations.get(agent.workflowId));
    builtGraphs.push(scoped);
    return scoped;
  }

  const roots = agents.filter(
    (agent) =>
      agent.role === "parent" ||
      !agent.parentWorkflowId ||
      !agentByWorkflow.has(agent.parentWorkflowId)
  );
  const rootGraphs = roots.map((agent) => buildNestedAgentGraph(agent));
  let nextX = 0;
  const positionedRoots = rootGraphs.map((graph) => {
    const bounds = graphBounds(graph);
    const positioned = offsetGraph(graph, nextX - bounds.minX, -bounds.minY);
    nextX += graphWidth(graph) + 95;
    return positioned;
  });
  const nodes = positionedRoots.flatMap((graph) => graph.nodes);
  const edges = positionedRoots.flatMap((graph) => graph.edges);

  return {
    nodes,
    edges,
    activeTurn: builtGraphs.find((graph) => graph.activeTurn != null)?.activeTurn ?? null,
    status: composeStatus(builtGraphs.map((graph) => graph.status)),
    failed: builtGraphs.some((graph) => graph.failed),
    usage: summarizeCost(agents.flatMap((agent) => agent.frames))
  };
}
