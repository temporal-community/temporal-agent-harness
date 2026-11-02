/**
 * Node positions in motion.
 *
 * The graph is a projection: every frame rebuilds the whole node array, so a
 * rearrangement the reader asked for — a tool batch folded, a batch opened, the
 * focus switch thrown — arrives as a wholly new set of positions and lands as a
 * jump cut. The nodes are the same nodes, and nothing on screen said so.
 *
 * Two rules for travel:
 *
 * - A node in both the old array and the new one travels between them. The
 *   travel is what says it is the same node in a new place.
 * - A node in only one of them does not travel. There is nowhere to come from,
 *   and a node sliding in from a position it never occupied is a lie about the
 *   run.
 *
 * Deciding *when* a rearrangement is worth animating is the caller's; this only
 * answers whether one happened at all, and what it looks like part-way through.
 * Between rearrangements, keepUnchanged hands the flow back what did not change.
 */

export interface TweenPosition {
  x: number;
  y: number;
}

export interface TweenNode {
  id: string;
  position: TweenPosition;
  /** Written by the flow onto the array it is bound to; see `lerpNodes`. */
  selected?: boolean;
}

/* What the flow writes onto the array it is bound to, which the projection never sets. */
const flowOwned = new Set(["measured", "selected", "dragging", "resizing"]);

function sameValue(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const left = a as Record<string, unknown>;
  const right = b as Record<string, unknown>;
  const keys = Object.keys(left);
  return (
    keys.length === Object.keys(right).length &&
    keys.every((key) => Object.hasOwn(right, key) && sameValue(left[key], right[key]))
  );
}

function sameElement(was: Record<string, unknown>, node: Record<string, unknown>): boolean {
  const keys = new Set([...Object.keys(was), ...Object.keys(node)]);
  for (const key of keys) {
    if (!flowOwned.has(key) && !sameValue(was[key], node[key])) return false;
  }
  return true;
}

/**
 * `next`, keeping every node or edge from `previous` that it did not change.
 *
 * xyflow skips a node or edge it is handed back by reference (adoptUserNodes,
 * getLayoutedEdges), and a card it skips does not re-render. A changed one still
 * keeps what the flow wrote on it, the measured size while its size holds, and
 * its `data` while that holds. Returns `previous` itself when nothing changed.
 */
export function keepUnchanged<T extends { id: string }>(previous: readonly T[], next: readonly T[]): T[] {
  if (previous.length === 0) return next as T[];
  const before = new Map(previous.map((item) => [item.id, item as Record<string, unknown>]));
  let changed = previous.length !== next.length;
  const kept = next.map((item, index) => {
    const was = before.get(item.id);
    const node = item as Record<string, unknown>;
    if (!was) {
      changed = true;
      return item;
    }
    if (sameElement(was, node)) {
      if (previous[index] !== was) changed = true;
      return was as T;
    }
    changed = true;
    const sameSize = was.width === node.width && was.height === node.height;
    return {
      ...item,
      ...(was.selected === undefined ? {} : { selected: was.selected }),
      ...(sameSize && was.measured !== undefined ? { measured: was.measured } : {}),
      ...("data" in node && sameValue(was.data, node.data) ? { data: was.data } : {})
    } as T;
  });
  return changed ? kept : (previous as T[]);
}

/** Whether something that stayed also moved, which is the only case worth animating. */
export function nodesMovedInPlace(
  previous: readonly TweenNode[],
  next: readonly TweenNode[]
): boolean {
  if (previous.length === 0) return false;
  const before = new Map(previous.map((node) => [node.id, node.position]));
  return next.some((node) => {
    const was = before.get(node.id);
    return was != null && (was.x !== node.position.x || was.y !== node.position.y);
  });
}

/**
 * The graph part-way from `previous` to `next`, at `t` in 0..1.
 *
 * Built on `next` rather than on `previous`, so the reading is current the whole
 * way: titles, tones and streamed result text arrive at full rate while the
 * boxes are still travelling. Only the position is interpolated, and only for a
 * node that exists in both.
 *
 * `selected` is carried across at every `t`, including 1. The flow writes it onto
 * the array it is bound to and the projection — which knows nothing about what
 * the reader clicked — does not, so replacing that array wholesale is what drops
 * the selection outline on every frame of a live run.
 */
export function lerpNodes<T extends TweenNode>(
  previous: readonly T[],
  next: readonly T[],
  t: number
): T[] {
  const before = new Map(previous.map((node) => [node.id, node]));
  return next.map((node) => {
    const was = before.get(node.id);
    if (!was) return node;
    const carried = was.selected === node.selected ? node : { ...node, selected: was.selected };
    if (t >= 1) return carried;
    return {
      ...carried,
      position: {
        x: was.position.x + (node.position.x - was.position.x) * t,
        y: was.position.y + (node.position.y - was.position.y) * t
      }
    };
  });
}
