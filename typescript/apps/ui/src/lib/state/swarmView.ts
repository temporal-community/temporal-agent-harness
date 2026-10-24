/**
 * The swarm pane's reading of a topology: which members and edges to draw, how each looks, and
 * the counts beside them. Pure, so the pane only draws what this decides.
 *
 * A swarm's shape (who started whom, and which board lane each member claims from) changes
 * slowly; its phases and peer edges change every poll. So the layout is keyed only on the
 * shape — `swarmShape` — and a poll that moved nothing but phases recolors in place instead of
 * shaking a settled graph.
 */

import type { SwarmEdge, SwarmNode, SwarmPhase, SwarmTopology } from "$lib/api/types";

/** Phases in the order the legend lists them: what is blocked on someone first. */
export const PHASE_ORDER: readonly SwarmPhase[] = [
  "awaiting_approval",
  "awaiting_callback",
  "awaiting_agent",
  "tool",
  "working",
  "idle"
];

export const PHASE_LABELS: Record<SwarmPhase, string> = {
  awaiting_approval: "needs approval",
  awaiting_callback: "awaiting callback",
  awaiting_agent: "waiting on an agent",
  tool: "running a tool",
  working: "working",
  idle: "idle"
};

/**
 * Each phase's hue, from the console's own tokens: --live is what the console already uses
 * for "needs you", and the event hues (--model, --tool, --reasoning) say what kind of work it
 * is. Idle is the quiet text tone; a member with no phase (a board lane) is --accent.
 */
export const PHASE_TONES: Record<SwarmPhase | "none", string> = {
  awaiting_approval: "--live",
  awaiting_callback: "--live",
  awaiting_agent: "--reasoning",
  tool: "--tool",
  working: "--model",
  idle: "--text-3",
  none: "--accent"
};

/** How big a member is drawn: the few that coordinate are large, the many workers small. */
const ROLE_RADIUS: Record<string, number> = {
  dispatcher: 14,
  board: 11,
  lead: 9,
  specialist: 7,
  reviewer: 7,
  worker: 4.5
};
const DEFAULT_RADIUS = 6;

export interface SwarmGraphNode {
  id: string;
  role: string;
  phase: SwarmPhase | null;
  closed: boolean;
  radius: number;
  /** CSS custom property for the node's color. */
  tone: string;
  label: string;
  node: SwarmNode;
}

export interface SwarmGraphEdge {
  id: string;
  source: string;
  target: string;
  kind: SwarmEdge["kind"];
  weight: number;
}

export interface SwarmGraph {
  nodes: SwarmGraphNode[];
  edges: SwarmGraphEdge[];
}

export function radiusOf(role: string | null): number {
  return ROLE_RADIUS[role ?? ""] ?? DEFAULT_RADIUS;
}

/** A workflow id short enough to label a node: its last segment, at most 12 characters. */
export function shortId(workflowId: string): string {
  const tail = workflowId.split(/[-:]/).filter(Boolean).at(-1) ?? workflowId;
  return tail.length > 12 ? tail.slice(0, 12) : tail;
}

/**
 * What to draw. Closed members are kept only when `showClosed` — a long run retires far more
 * agents than it keeps — and an edge is drawn only when both its ends are.
 */
export function swarmGraph(
  topology: SwarmTopology,
  { showClosed = false }: { showClosed?: boolean } = {}
): SwarmGraph {
  const nodes = topology.nodes
    .filter((node) => showClosed || !node.closed)
    .map((node) => ({
      id: node.workflow_id,
      role: node.role ?? "member",
      phase: node.phase,
      closed: node.closed,
      radius: radiusOf(node.role),
      tone: node.closed ? "--text-4" : PHASE_TONES[node.phase ?? "none"],
      label: node.role === "board" ? `board · ${shortId(node.workflow_id)}` : node.role ?? shortId(node.workflow_id),
      node
    }));
  const shown = new Set(nodes.map((node) => node.id));
  const edges = topology.edges
    .filter((edge) => shown.has(edge.source) && shown.has(edge.target))
    .map((edge) => ({
      id: `${edge.kind}:${edge.source}>${edge.target}`,
      source: edge.source,
      target: edge.target,
      kind: edge.kind,
      weight: edge.weight
    }));
  return { nodes, edges };
}

/**
 * The part of a graph the layout depends on: its members and its parent and board edges.
 * Peer edges come and go every poll, so they pull on the layout without restarting it.
 */
export function swarmShape({ nodes, edges }: SwarmGraph): string {
  const structural = edges.filter((edge) => edge.kind !== "peer").map((edge) => edge.id);
  return `${nodes.map((node) => node.id).join("\n")}\n\n${structural.join("\n")}`;
}

export interface SwarmSummary {
  running: number;
  closed: number;
  /** Running members per phase, in legend order, zeros dropped. */
  phases: { phase: SwarmPhase; count: number }[];
  /** Members per role, most first. */
  roles: { role: string; count: number }[];
  /** Board items per status, summed over the lanes read. */
  board: { open: number; claimed: number; done: number } | null;
  peerEdges: number;
}

export function swarmSummary(topology: SwarmTopology): SwarmSummary {
  const running = topology.nodes.filter((node) => !node.closed).length;
  const phases = PHASE_ORDER.map((phase) => ({ phase, count: topology.phases[phase] ?? 0 })).filter(
    (entry) => entry.count > 0
  );
  const roles = Object.entries(topology.roles)
    .map(([role, count]) => ({ role, count }))
    .sort((a, b) => b.count - a.count || a.role.localeCompare(b.role));
  const board =
    topology.lanes.length === 0
      ? null
      : topology.lanes.reduce(
          (sum, lane) => ({
            open: sum.open + lane.counts.open,
            claimed: sum.claimed + lane.counts.claimed,
            done: sum.done + lane.counts.done
          }),
          { open: 0, claimed: 0, done: 0 }
        );
  return {
    running,
    closed: topology.nodes.length - running,
    phases,
    roles,
    board,
    peerEdges: topology.edges.filter((edge) => edge.kind === "peer").length
  };
}

/**
 * The members that need someone, most stale first: what the console should point at in a swarm
 * of hundreds. Only the blocked-on-a-human phases count.
 */
export function needsAttention(topology: SwarmTopology): SwarmNode[] {
  return topology.nodes
    .filter(
      (node) =>
        !node.closed && (node.phase === "awaiting_approval" || node.phase === "awaiting_callback")
    )
    .sort((a, b) => (a.last_active ?? a.start_time) - (b.last_active ?? b.start_time));
}
