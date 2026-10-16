/**
 * The keyboard half of every resize gutter: a pane's width handle and both docked
 * drawers read keys through this, so the step and the modifier rule are one rule.
 */
export const RESIZE_STEP = 24;

/** Which arrow pair moves a gutter — the pair along the axis it slides on. */
export type ResizeKeys = "left-right" | "up-down";

export type DrawerEdge = "bottom" | "left";

/**
 * What a key on a gutter asks for: a step in px, "reset" for Home, or null for a key
 * that is not the gutter's. Modified arrows are never the gutter's — they belong to
 * the rail, which walks and reorders panes with them.
 */
export function resizeKeyIntent(
  event: Pick<KeyboardEvent, "key" | "altKey" | "metaKey" | "ctrlKey" | "shiftKey">,
  keys: ResizeKeys
): number | "reset" | null {
  if (event.altKey || event.metaKey || event.ctrlKey || event.shiftKey) return null;
  if (event.key === "Home") return "reset";
  const [shrink, grow] = keys === "left-right" ? ["ArrowLeft", "ArrowRight"] : ["ArrowDown", "ArrowUp"];
  if (event.key === shrink) return -RESIZE_STEP;
  if (event.key === grow) return RESIZE_STEP;
  return null;
}

/** A bottom drawer grows upward, a left drawer rightward. */
export function drawerResizeKeys(edge: DrawerEdge): ResizeKeys {
  switch (edge) {
    case "bottom":
      return "up-down";
    case "left":
      return "left-right";
    default: {
      const _exhaustive: never = edge;
      throw new Error(`unhandled drawer edge: ${_exhaustive}`);
    }
  }
}

/** A drawer below its minimum is shut, and none is taller or wider than its maximum. */
export function clampDrawerSize(size: number, minSize: number, maxSize: number): number {
  return size < minSize ? 0 : Math.min(maxSize, size);
}

/**
 * A size saved by an earlier drag, or null when there is none worth restoring. A saved
 * shut drawer is not a size anyone wants back, and a size saved in a larger window is
 * brought inside this one's maximum.
 */
export function restoredDrawerSize(saved: unknown, minSize: number, maxSize: number): number | null {
  if (typeof saved !== "number" || !Number.isFinite(saved) || saved < minSize) return null;
  return Math.max(minSize, Math.min(maxSize, saved));
}

/**
 * One key step. The same snap as a drag, except that growing a drawer that is shut
 * opens it at its minimum — a step smaller than the minimum would otherwise snap it
 * straight back, and the keyboard could never reopen it.
 */
export function stepDrawerSize(
  size: number,
  step: number,
  minSize: number,
  maxSize: number
): number {
  const next = size + step;
  if (step > 0 && next < minSize) return Math.min(maxSize, minSize);
  return clampDrawerSize(next, minSize, maxSize);
}
