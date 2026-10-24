/**
 * Where the 3D swarm view puts each member, apart from any renderer so it can be tested.
 *
 * The plane is laid out by a d3-force simulation, the same forces as the 2D view: members are
 * pulled toward their parent and pushed apart. The third axis — up, in the scene — is chosen by
 * the mode, and is what the 3D view adds:
 *
 *   - "constellation": up is depth in the hierarchy. The dispatcher floats on top, its leads one
 *     tier below, their workers below that, so each team hangs as a cone under its lead. Board
 *     lanes have no parent; they ride on a tier of their own between the coordinators and the
 *     workers, where the claims to them read as threads.
 *   - "growth": up is when the member started. The swarm becomes a tree growing upward: a wave
 *     of fan-out is a band, and worker churn is the column of retired members under each slot.
 *
 * Everything is keyed by workflow id, so a member keeps its place from one poll to the next and
 * a new one starts beside its parent.
 */

import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum
} from "d3-force";

import { seededRandom } from "./okfGraph";
import { swarmShape, type SwarmGraph, type SwarmGraphEdge } from "./swarmView";

export type SceneMode = "constellation" | "growth";

export type Vec3 = [number, number, number];

interface PlanarNode extends SimulationNodeDatum {
  id: string;
  parentId: string | null;
  radius: number;
}
type PlanarLink = SimulationLinkDatum<PlanarNode> & { kind: SwarmGraphEdge["kind"] };

const LINK_DISTANCE: Record<SwarmGraphEdge["kind"], number> = { parent: 30, peer: 90, board: 90 };
const LINK_STRENGTH: Record<SwarmGraphEdge["kind"], number> = { parent: 0.7, peer: 0.03, board: 0.05 };

/**
 * The plane: a d3-force simulation the caller steps (once per animation frame, or all at once
 * in a test), never running on a timer of its own.
 */
export class PlanarLayout {
  readonly #placed = new Map<string, PlanarNode>();
  readonly #simulation: Simulation<PlanarNode, PlanarLink>;
  #shape = "";

  constructor(seed = 11) {
    this.#simulation = forceSimulation<PlanarNode, PlanarLink>()
      .randomSource(seededRandom(seed))
      .force("charge", forceManyBody<PlanarNode>().strength(-50).distanceMax(300))
      .force("collide", forceCollide<PlanarNode>((d) => d.radius + 3))
      .force("x", forceX<PlanarNode>(0).strength(0.03))
      .force("y", forceY<PlanarNode>(0).strength(0.03))
      .stop();
  }

  /** Take a new graph. Returns whether its shape changed, i.e. the layout was reheated. */
  update(graph: SwarmGraph): boolean {
    const live = new Set(graph.nodes.map((n) => n.id));
    for (const id of [...this.#placed.keys()]) if (!live.has(id)) this.#placed.delete(id);
    const nodes = graph.nodes.map((node, index) => {
      const existing = this.#placed.get(node.id);
      if (existing) {
        existing.radius = node.radius;
        existing.parentId = node.node.parent_id;
        return existing;
      }
      const parent = node.node.parent_id ? this.#placed.get(node.node.parent_id) : undefined;
      const angle = index * 2.399963;
      const ring = parent ? 20 : 14 * Math.sqrt(index + 0.5);
      const created: PlanarNode = {
        id: node.id,
        parentId: node.node.parent_id,
        radius: node.radius,
        x: (parent?.x ?? 0) + ring * Math.cos(angle),
        y: (parent?.y ?? 0) + ring * Math.sin(angle)
      };
      this.#placed.set(node.id, created);
      return created;
    });
    this.#simulation.nodes(nodes);
    this.#simulation.force(
      "link",
      forceLink<PlanarNode, PlanarLink>(
        graph.edges.map((e) => ({ source: e.source, target: e.target, kind: e.kind }))
      )
        .id((d) => d.id)
        .distance((l) => LINK_DISTANCE[l.kind])
        .strength((l) => LINK_STRENGTH[l.kind])
    );
    const shape = swarmShape(graph);
    if (shape === this.#shape) return false;
    this.#shape = shape;
    this.#simulation.alpha(Math.max(this.#simulation.alpha(), 0.3));
    return true;
  }

  /** Step the simulation unless it has settled. Returns whether anything moved. */
  tick(steps = 1): boolean {
    if (this.#simulation.alpha() < this.#simulation.alphaMin()) return false;
    this.#simulation.tick(steps);
    return true;
  }

  get settled(): boolean {
    return this.#simulation.alpha() < this.#simulation.alphaMin();
  }

  position(id: string): { x: number; y: number } | null {
    const node = this.#placed.get(id);
    return node && node.x !== undefined && node.y !== undefined ? { x: node.x, y: node.y } : null;
  }
}

/** Each shown member's depth below the swarm's roots, following parent links. */
export function depths(graph: SwarmGraph): Map<string, number> {
  const parent = new Map(graph.nodes.map((n) => [n.id, n.node.parent_id]));
  const memo = new Map<string, number>();
  const depth = (id: string, guard = 0): number => {
    const known = memo.get(id);
    if (known !== undefined) return known;
    const up = parent.get(id);
    const value = up && parent.has(up) && guard < 64 ? depth(up, guard + 1) + 1 : 0;
    memo.set(id, value);
    return value;
  };
  for (const node of graph.nodes) depth(node.id);
  return memo;
}

export interface SceneOptions {
  /** Height between hierarchy tiers, in scene units (constellation). */
  tier?: number;
  /** Total height the run's start times are spread over (growth). */
  height?: number;
}

/**
 * Where each member should be, as [x, up, z]: the plane's x and y become the scene's x and z,
 * and up comes from the mode.
 */
export function targets(
  graph: SwarmGraph,
  layout: PlanarLayout,
  mode: SceneMode,
  { tier = 70, height = 360 }: SceneOptions = {}
): Map<string, Vec3> {
  const out = new Map<string, Vec3>();
  const depthOf = depths(graph);
  const deepest = Math.max(0, ...depthOf.values());
  const starts = graph.nodes.map((n) => n.node.start_time);
  const first = Math.min(...starts);
  const span = Math.max(1e-6, Math.max(...starts) - first);
  for (const node of graph.nodes) {
    const at = layout.position(node.id) ?? { x: 0, y: 0 };
    let up: number;
    if (mode === "growth") {
      up = ((node.node.start_time - first) / span - 0.5) * height;
    } else if (node.role === "board") {
      // Half a tier above the deepest (the workers), so claims read as short threads up to it.
      up = (deepest / 2 - Math.max(0.5, deepest - 0.5)) * tier;
    } else {
      up = (deepest / 2 - (depthOf.get(node.id) ?? 0)) * tier;
    }
    out.set(node.id, [at.x, up, at.y]);
  }
  return out;
}

/** Line-segment vertices (x, y, z pairs) for the edges of `kinds` whose ends are both placed. */
export function segments(
  edges: readonly SwarmGraphEdge[],
  at: ReadonlyMap<string, Vec3>,
  kinds: ReadonlySet<SwarmGraphEdge["kind"]>
): Float32Array {
  const chosen = edges.filter((e) => kinds.has(e.kind) && at.has(e.source) && at.has(e.target));
  const out = new Float32Array(chosen.length * 6);
  chosen.forEach((edge, i) => {
    out.set(at.get(edge.source)!, i * 6);
    out.set(at.get(edge.target)!, i * 6 + 3);
  });
  return out;
}

/** A stable fraction in [0, 1) for a string, so each edge's particles run out of step. */
export function phaseOf(id: string): number {
  let hash = 2166136261;
  for (let i = 0; i < id.length; i++) hash = Math.imul(hash ^ id.charCodeAt(i), 16777619);
  return (hash >>> 0) / 4294967296;
}

/**
 * Points travelling along each peer edge, from the asker toward the peer: `perEdge` of them,
 * evenly spaced, at time `t` seconds (a full trip every `period` seconds).
 */
export function peerParticles(
  edges: readonly SwarmGraphEdge[],
  at: ReadonlyMap<string, Vec3>,
  t: number,
  { perEdge = 3, period = 1.6 }: { perEdge?: number; period?: number } = {}
): Float32Array {
  const peers = edges.filter((e) => e.kind === "peer" && at.has(e.source) && at.has(e.target));
  const out = new Float32Array(peers.length * perEdge * 3);
  peers.forEach((edge, i) => {
    const [ax, ay, az] = at.get(edge.source)!;
    const [bx, by, bz] = at.get(edge.target)!;
    for (let k = 0; k < perEdge; k++) {
      const f = (t / period + phaseOf(edge.id) + k / perEdge) % 1;
      out.set([ax + (bx - ax) * f, ay + (by - ay) * f, az + (bz - az) * f], (i * perEdge + k) * 3);
    }
  });
  return out;
}

/** Move `current` a `fraction` of the way toward `target`, in place; new members jump. */
export function ease(
  current: Map<string, Vec3>,
  target: ReadonlyMap<string, Vec3>,
  fraction: number
): void {
  for (const id of [...current.keys()]) if (!target.has(id)) current.delete(id);
  for (const [id, goal] of target) {
    const now = current.get(id);
    if (!now) {
      current.set(id, [...goal]);
      continue;
    }
    for (let axis = 0; axis < 3; axis++) now[axis] += (goal[axis] - now[axis]) * fraction;
  }
}
