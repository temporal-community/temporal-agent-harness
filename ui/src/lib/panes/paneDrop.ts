/**
 * Where a dragged pane will land, decided from where the pointer sits in the
 * column it is over.
 *
 * Five outcomes, no modifier key. The left and right edges mean "a column of
 * your own, this side of me". The top and bottom edges split this column, tmux
 * style, and both panes stay on screen. The middle — the biggest target, because
 * it is the one that is new — means "share this column with me as tabs".
 *
 * A modifier was the wrong shape for this: the gesture that needs discovering is
 * the one that shares a column, and a key you have to be told about is the one
 * nobody finds. Edges-and-middle is what every editor with a split view uses, so
 * it is a gesture people arrive already knowing.
 */
export type PaneDropEdge = "before" | "after" | "above" | "below" | "tab";

/**
 * How much of a column each edge zone takes. Wide enough to hit without aiming,
 * narrow enough to leave the middle the majority of the target. The vertical
 * bands are the more generous pair because a column is far taller than it is
 * wide, so the same fraction is a much bigger distance to travel.
 */
const SIDE_BAND = 0.22;
const END_BAND = 0.25;

/** The least a pane may be squeezed to in a split column. */
export const SPLIT_MIN = 120;

/**
 * Sideways is asked of the column and up-and-down of the pane under the pointer,
 * because the two answers are about different things: left and right place a
 * pane against the whole column, while top and bottom place it against the pane
 * it is pointing at. Measuring both on the column would make the seam inside an
 * already-split column unreachable — the only place left to aim is its middle,
 * which means tabs.
 */
export function dropEdgeAt(
  clientX: number,
  clientY: number,
  column: DOMRectReadOnly,
  pane: DOMRectReadOnly = column
): PaneDropEdge {
  if (column.width <= 0 || pane.height <= 0) return "after";
  /* Sideways first, so the corners go to the rail: landing a pane beside a column
     is the coarser intent, and it is what a reader aiming roughly at the gap
     between two columns means. */
  const across = (clientX - column.left) / column.width;
  if (across < SIDE_BAND) return "before";
  if (across > 1 - SIDE_BAND) return "after";
  const down = (clientY - pane.top) / pane.height;
  if (down < END_BAND) return "above";
  if (down > 1 - END_BAND) return "below";
  return "tab";
}

export function dropEdgeLabel(edge: PaneDropEdge): string {
  switch (edge) {
    case "before":
      return "Left";
    case "after":
      return "Right";
    case "above":
      return "Top";
    case "below":
      return "Bottom";
    default:
      return "Tab";
  }
}

/** Whether the landing shares the target's column rather than making a new one. */
export function edgeShares(edge: PaneDropEdge): boolean {
  return edge === "tab" || edge === "above" || edge === "below";
}
