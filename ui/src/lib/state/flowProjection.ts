import type { Edge, Node } from "@xyflow/svelte";
import type {
  AgentInterfaceFunction,
  AgentSseFrame,
  OperatorCommand,
  ToolId
} from "$lib/api/types";
import { formatTokens, summarizeCost, type CostSummary } from "$lib/cost/pricing";
import { renderUserMessage } from "$lib/state/inboundMessageText";
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
  dotTone?: AgentNodeTone;
  title: string;
  state: string;
  subtitle?: string;
  detail?: string;
  active?: boolean;
  size?: "default" | "large" | "container";
  runtimeRole?: "parent" | "subagent";
  statusTone?: AgentNodeTone;
  approvalPort?: boolean;
  approvalDecisionPort?: boolean;
  nodeWidth?: number;
  nodeHeight?: number;
  flowGroup?: number;
  metrics?: Array<{ label: string; value: string }>;
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
  kind: "text" | "code" | "json";
}

export interface AgentGraph {
  nodes: Node<AgentNodeData>[];
  edges: Edge[];
  activeTurn: number | null;
  status: "idle" | "running" | "replied" | "error";
  usage: CostSummary;
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
  operatorInterface?: OperatorCommand[];
  stopped?: boolean;
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

interface AgentGraphOptions {
  inputPlacement?: "external" | "runtime";
  showSubagentDispatch?: boolean;
  outputPlacement?: "external" | "runtime";
  agentInterface?: AgentInterfaceFunction[];
  embeddedToolGraphs?: AgentGraph[];
}

type RuntimeNodeId =
  | "input"
  | "model"
  | "reasoning"
  | "tool-container"
  | "subagent"
  | "output"
  | ToolRuntimeNodeId;

type ToolRuntimeNodeId = `tool:${ToolId}`;

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
const codeModeHeaderHeight = 126;
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
    draggable: false,
    selectable: type !== "agentWorkflow",
    zIndex: type === "agentWorkflow" ? 0 : 10
  };
}

function edge(
  id: string,
  source: string,
  target: string,
  animated = false,
  label?: string,
  options: {
    sourceHandle?: string;
    targetHandle?: string;
    kind?: EdgeKind;
    edgeType?: string;
  } = {}
): Edge {
  const kind = options.kind ?? "main";
  return {
    id,
    source,
    target,
    animated,
    label,
    sourceHandle: options.sourceHandle ?? "source-right",
    targetHandle: options.targetHandle ?? "target-left",
    type: options.edgeType ?? "step",
    class: `edge-${kind}`,
    zIndex: 1
  };
}

function scopedId(workflowId: string, localId: string): string {
  return `${workflowId}::${localId}`;
}

function toolRuntimeNodeId(toolId: ToolId): ToolRuntimeNodeId {
  return `tool:${toolId}`;
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
  let contentWidth = stateNodeWidth;
  let contentHeight = stateNodeHeight;

  for (let slot = 0; slot < flowOrder.length; slot += 1) {
    const dimensions = sizes[slot];
    const col = slot % layout.columns;
    const row = Math.floor(slot / layout.columns);
    const x = layout.gridStartX + col * (stateNodeWidth + runtimeColumnGap);
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

function layoutEmbeddedToolGraphs(graphs: AgentGraph[]): EmbeddedToolLayout | null {
  if (graphs.length === 0) return null;
  let nextY = embeddedToolHeaderHeight;
  let maxWidth = 0;
  const placements: EmbeddedToolPlacement[] = [];
  for (const graph of graphs) {
    const bounds = graphBounds(graph);
    placements.push({
      graph,
      x: embeddedToolPadding - bounds.minX,
      y: nextY - bounds.minY
    });
    nextY += bounds.height + embeddedToolGap;
    maxWidth = Math.max(maxWidth, bounds.width);
  }
  const contentHeight = nextY - embeddedToolGap + embeddedToolPadding;
  return {
    dimensions: {
      width: Math.max(stateNodeWidth, maxWidth + embeddedToolPadding * 2),
      height: contentHeight
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
    edges: graph.edges.map((item) => ({ ...item }))
  };
}

function runtimeEdgeOptions(
  source: string,
  target: string,
  runtimeLayout: RuntimeLayout
): Parameters<typeof edge>[5] {
  const sourcePosition = runtimeLayout.positions.get(source as RuntimeNodeId);
  const targetPosition = runtimeLayout.positions.get(target as RuntimeNodeId);
  return sourcePosition && targetPosition && sourcePosition.y !== targetPosition.y
    ? { sourceHandle: "source-bottom", targetHandle: "target-top", kind: "main" }
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
  inputPlacement: AgentGraphOptions["inputPlacement"],
  latestNodeId: LocalNodeId | null
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
              latestNodeId === target,
              undefined,
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

function valueSection(label: string, value: unknown): AgentNodeContext | null {
  if (value == null) return null;
  return { label, text: JSON.stringify(value, null, 2), kind: "json" };
}

export function buildAgentGraph(
  frames: AgentSseFrame[],
  options: AgentGraphOptions = {}
): AgentGraph {
  const inputPlacement = options.inputPlacement ?? "external";
  const showSubagentDispatch = options.showSubagentDispatch ?? true;
  const outputPlacement = options.outputPlacement ?? "external";
  const agentInterface = summarizeAgentInterface(options.agentInterface);
  let activeTurn: number | null = null;
  let status: AgentGraph["status"] = "idle";
  let currentUserMessage = "No message received";
  let queuedMessage = "";
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
  let replyState = "waiting";
  let queued = 0;
  let runtimeHeaderPrefix: string | undefined;
  let subagentState = "idle";
  let subagentSubtitle = "";
  let subagentDetail = "";
  const tools = new Map<ToolId, ToolRuntime>();
  const codeModeChildren = new Map<ToolId, ToolId[]>();
  const activeCodeModeToolIds: ToolId[] = [];
  const activeRuntimeToolIds = new Set<ToolId>();
  let runtimeToolFlowGroup = 0;
  const runtimeNodeOrder: RuntimeNodeId[] = [];
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
    if (!runtimeNodeOrder.includes(id)) runtimeNodeOrder.push(id);
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

  function markToolSettled(toolId: ToolId, parentToolId?: ToolId): void {
    if (!parentToolId) activeRuntimeToolIds.delete(toolId);
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

  function codeModeHostChildIds(toolId: ToolId): ToolId[] {
    return (codeModeChildren.get(toolId) ?? []).filter((childId) => tools.has(childId));
  }

  function childToolParent(toolId: ToolId, isCodeModeTool: boolean): ToolId | undefined {
    if (isCodeModeTool) return undefined;
    return tools.get(toolId)?.parentToolId ?? activeCodeModeToolIds.at(-1);
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
    codeModeChildren.clear();
    activeCodeModeToolIds.splice(0, activeCodeModeToolIds.length);
    activeRuntimeToolIds.clear();
    runtimeToolFlowGroup = 0;
    for (let index = runtimeNodeOrder.length - 1; index >= 0; index -= 1) {
      if (isToolRuntimeNodeId(runtimeNodeOrder[index])) {
        runtimeNodeOrder.splice(index, 1);
      }
    }
  }

  for (const frame of frames) {
    if (!("type" in frame.data)) continue;
    currentFrame = frame;
    if (frame.event === "message_queued") {
      markInput();
      queued += 1;
      queuedMessage = frame.data.user_message;
      currentUserMessage = frame.data.user_message;
      inputState = `${queued} queued`;
    }
    if (frame.event === "turn_started") {
      markInput();
      activeTurn = frame.data.turn_number;
      status = "running";
      currentUserMessage = frame.data.user_message;
      inputState = `turn ${frame.data.turn_number}`;
      modelState = "waiting";
      reasoningState = "waiting";
      reasoningDetail = "";
      reasoningStartTs = null;
      reasoningEndTs = null;
      replyText = "";
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
      replyState = "streaming";
    } else if (frame.event === "text_annotation") {
      markOutput();
      replyState = "annotated";
    } else if (frame.event === "reply") {
      markOutput();
      status = "replied";
      replyText = textFromReply(frame.data) || replyText;
      replyState = "reply available";
    } else if (frame.event === "error") {
      markOutput();
      status = "error";
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
        if (!frame.data.approved) markToolSettled(frame.data.tool_id, parentToolId);
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
        subagentState = `${frame.data.function} → turn ${frame.data.subagent_turn}`;
      } else if (frame.event === "subagent_reply_received") {
        subagentState = `reply ${frame.data.outcome}`;
      } else if (frame.event === "subagent_stream_unavailable") {
        subagentState = "detail unavailable";
      } else {
        subagentState = "stopped";
      }
    }
  }

  const retryRedirect = collapseConsecutiveFailedRetries(runtimeNodeOrder, tools);
  if (latestNodeId && retryRedirect.has(latestNodeId)) {
    latestNodeId = retryRedirect.get(latestNodeId)!;
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
  const reasoningSummary =
    reasoningDetail || (runtimeNodeOrder.includes("reasoning") ? NO_THOUGHT_SUMMARY : "");
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
  function contextFor(id: LocalNodeId): AgentNodeContext[] {
    const sections: Array<AgentNodeContext | null> = [];

    if (id === "input") {
      const raw = currentUserMessage || queuedMessage;
      const rendered = renderUserMessage(raw);
      sections.push(textSection("User message", rendered));
      if (rendered !== raw) sections.push(maybeJsonSection("Raw message", raw));
    } else if (id === "model") {
      /* The model's own output and its thinking — the two things asked for by
         name. Both were already in this scope; nothing reached them because the
         inspector only ever read `detail`, and this node has never had one. */
      sections.push(
        textSection("Model output", replyText),
        textSection("Thought summary", reasoningSummary),
        textSection("Model", currentModel)
      );
    } else if (id === "reasoning") {
      sections.push(textSection("Thought summary", reasoningSummary));
    } else if (id === "output") {
      sections.push(textSection("Reply", replyText));
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
        detail: renderUserMessage(currentUserMessage || queuedMessage),
        nodeHeight: resultNodeHeight,
        active: latestNodeId === id
      };
    }
    if (id === "model") {
      return {
        tone: modelState === "running" ? "model" : "neutral",
        dotTone: "model",
        title: "Model interaction",
        state: modelState,
        subtitle: currentModel,
        active: latestNodeId === id,
        size: "large",
        metrics: [
          { label: "input", value: formatTokens(usage.tokens.input) },
          { label: "output", value: formatTokens(usage.tokens.output) }
        ]
      };
    }
    if (id === "reasoning") {
      return {
        tone:
          reasoningState === "running" || reasoningState === "captured"
            ? "reasoning"
            : "neutral",
        dotTone: "reasoning",
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
        dotTone: "tool",
        title: runtime?.name ?? "Tool",
        state: retryCount > 1 ? `${toolStatus} ×${retryCount}` : toolStatus,
        statusTone: runtime?.statusTone,
        subtitle: runtime?.subtitle ?? "tool lifecycle",
        detail,
        size: codeModeDimensions ? "container" : undefined,
        nodeWidth: codeModeDimensions?.width,
        nodeHeight: codeModeDimensions?.height ?? resultNodeHeight,
        active: latestNodeId === id,
        toolId: runtime?.id,
        codeMode: runtime?.isCodeMode,
        flowGroup: runtime?.flowGroup
      };
    }
    if (id === "tool-container") {
      return {
        tone: "tool",
        dotTone: "tool",
        title: "Subagent activity",
        state: "delegating",
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
          subagentState === "detail unavailable"
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
  for (const [parentToolId, childToolIds] of codeModeChildren) {
    const parentNodeId = toolRuntimeNodeId(parentToolId);
    const parentPosition = runtimeLayout.positions.get(parentNodeId);
    if (!parentPosition) continue;
    for (const [index, childToolId] of childToolIds.entries()) {
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
    }
  }
  if (outputSeen && outputPlacement === "external") {
    nodes.push(node("output", outputPosition(boundaryWidth), nodeDataFor("output")));
  }

  const edges: Edge[] = [];
  const toolPosition = runtimeLayout.positions.get("tool-container");
  if (toolPosition && embeddedToolLayout) {
    for (const placement of embeddedToolLayout.placements) {
      const embeddedGraph = offsetGraph(
        placement.graph,
        toolPosition.x + placement.x,
        toolPosition.y + placement.y,
        4
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
        latestNodeId === "reasoning",
        undefined,
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
    inputPlacement,
    latestNodeId
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
            latestNodeId === "output",
            undefined,
            { kind: "output" }
          )
        )
      );
    } else if (inputSeen) {
      edges.push(
        edge("flow-input-output", "input", "output", latestNodeId === "output", undefined, {
          kind: "output"
        })
      );
    }
  }

  return { nodes, edges, activeTurn, status, usage };
}

function graphSourceLabel(agent: AgentGraphSource): string {
  if (agent.role === "parent") return "Parent agent";
  return `${agent.agentKey ?? "subagent"} subagent`;
}

function graphSourceSubtitle(agent: AgentGraphSource): string {
  const bits = [agent.label];
  if (agent.subagentId) bits.unshift(`subagent ${agent.subagentId}`);
  if (agent.stopped) bits.push("stopped");
  return bits.filter(Boolean).join(" · ");
}

function scopedGraph(
  agent: AgentGraphSource,
  graph: AgentGraph,
  xOffset: number,
  yOffset: number
): AgentGraph {
  const scopedNodeId = (id: string) =>
    id.includes("::") ? id : scopedId(agent.workflowId, id);
  const nodes = graph.nodes.map((item) => ({
    ...item,
    id: scopedNodeId(item.id),
    position: {
      x: item.position.x + xOffset,
      y: item.position.y + yOffset
    },
    data:
      item.id === "agent-runtime"
        ? {
            ...item.data,
            tone: agent.stopped ? "done" : item.data.tone,
            runtimeRole: agent.role,
            state: agent.stopped ? "stopped" : item.data.state,
            title: `${graphSourceLabel(agent)} runtime`,
            subtitle: [graphSourceSubtitle(agent), item.data.subtitle]
              .filter(Boolean)
              .join(" · ")
          }
        : item.data
  }));
  const edges = graph.edges.map((item) => ({
    ...item,
    id: item.id.includes("::") ? item.id : scopedId(agent.workflowId, item.id),
    source: scopedNodeId(item.source),
    target: scopedNodeId(item.target)
  }));

  return { ...graph, nodes, edges };
}

function graphWidth(graph: AgentGraph): number {
  return graphBounds(graph).width;
}

function composeStatus(statuses: AgentGraph["status"][]): AgentGraph["status"] {
  if (statuses.includes("error")) return "error";
  if (statuses.includes("running")) return "running";
  if (statuses.includes("replied")) return "replied";
  return "idle";
}

export function buildAgentTreeGraph(agents: AgentGraphSource[]): AgentGraph {
  if (agents.length === 0) return buildAgentGraph([]);

  const agentByWorkflow = new Map(agents.map((agent) => [agent.workflowId, agent]));
  const childrenByParent = new Map<string, AgentGraphSource[]>();
  for (const agent of agents) {
    if (!agent.parentWorkflowId) continue;
    const children = childrenByParent.get(agent.parentWorkflowId) ?? [];
    children.push(agent);
    childrenByParent.set(agent.parentWorkflowId, children);
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
      embeddedToolGraphs: childGraphs
    });
    const scoped = scopedGraph(agent, graph, 0, 0);
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
    usage: summarizeCost(agents.flatMap((agent) => agent.frames))
  };
}
