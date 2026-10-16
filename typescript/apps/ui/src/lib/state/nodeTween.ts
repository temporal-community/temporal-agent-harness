/**
 * Node positions in motion.
 *
 * The graph is a projection: every frame rebuilds the whole node array, so a
 * rearrangement the reader asked for — a tool batch folded, a batch opened, the
 * focus switch thrown — arrives as a wholly new set of positions and lands as a
 * jump cut. The nodes are the same nodes, and nothing on screen said so.
 *
 * Two rules, and they are the whole module:
 *
 * - A node in both the old array and the new one travels between them. The
 *   travel is what says it is the same node in a new place.
 * - A node in only one of them does not travel. There is nowhere to come from,
 *   and a node sliding in from a position it never occupied is a lie about the
 *   run.
 *
 * Deciding *when* a rearrangement is worth animating is the caller's; this only
 * answers whether one happened at all, and what it looks like part-way through.
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
