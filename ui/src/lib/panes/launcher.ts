/**
 * The pane launcher as a list of switches: which views are open, what a press
 * on one does, and what a key does to the list.
 *
 * DOM-free for the reason `quickSwitch.ts` is: these are the decisions, and the
 * tests can drive them without a browser. `PaneMinimap` wires them up.
 *
 * A view is one id — `logs`, not `logs:wf-123`. The launcher only ever opens the
 * unscoped view, and a scoped one is a drill-in that belongs to wherever it was
 * opened from, so every row here is single-instance even for the kinds that can
 * be scoped more than once.
 */
import type { PaneStack } from "$lib/state/paneStack.svelte";
import { PANE_KINDS, type PaneKind } from "./registry";

export interface LauncherItem {
  kind: PaneKind;
  /** Open in any of the stacks — the rail, a tab or split in it, or the drawer. */
  open: boolean;
  /** Why it cannot be switched off right now, or null when it can. Only ever set on an open view. */
  locked: string | null;
}

export const PINNED_REASON = "Pinned — unpin it to close";

/** Every kind, in registry order, so a row never moves under the pointer. */
export function launcherItems(stacks: readonly PaneStack[]): LauncherItem[] {
  return PANE_KINDS.map((kind) => {
    const holding = stacks.filter((stack) => stack.has(kind));
    /* Any pinned copy locks the row: closing only the others would leave it
       checked, which reads as a press that did nothing. */
    const locked = holding.some((stack) => !stack.canClose(kind)) ? PINNED_REASON : null;
    return { kind, open: holding.length > 0, locked };
  });
}

/**
 * Open a closed view beside the focused pane on `rail`, or close an open one
 * everywhere it is. A locked view is left alone; the row says why.
 */
export function toggleView(kind: PaneKind, rail: PaneStack, stacks: readonly PaneStack[]): void {
  const holding = stacks.filter((stack) => stack.has(kind));
  if (holding.length === 0) {
    rail.openPane({ kind }, rail.focusedId);
    return;
  }
  if (holding.some((stack) => !stack.canClose(kind))) return;
  for (const stack of holding) stack.closePane(kind);
}

type LauncherKeyAction = { kind: "move"; index: number } | { kind: "toggle" };

export interface LauncherKeyEvent {
  key: string;
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
}

/* The WAI-ARIA menu pattern, wrapping at the ends. Escape is not here: the
   shared `dismissable` attachment already answers it for every popover. */
function launcherKeyAction(event: LauncherKeyEvent, index: number, count: number): LauncherKeyAction | null {
  if (count === 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return null;
  switch (event.key) {
    case "ArrowDown":
      return { kind: "move", index: (index + 1) % count };
    case "ArrowUp":
      return { kind: "move", index: (index - 1 + count) % count };
    case "Home":
      return { kind: "move", index: 0 };
    case "End":
      return { kind: "move", index: count - 1 };
    case "Enter":
    case " ":
      return { kind: "toggle" };
    default:
      return null;
  }
}

export interface LauncherKeyHandlers {
  move(index: number): void;
  toggle(index: number): void;
}

/**
 * The menu's keydown, whole. A key it acts on is `preventDefault`ed, which keeps
 * the window's replay keys (Home, End, Space) from firing on the same press and
 * stops the button's own Enter/Space click from toggling a second time.
 */
export function handleLauncherKey(
  event: LauncherKeyEvent & { preventDefault(): void },
  index: number,
  count: number,
  handlers: LauncherKeyHandlers
): void {
  const action = launcherKeyAction(event, index, count);
  if (!action) return;
  event.preventDefault();
  switch (action.kind) {
    case "move":
      handlers.move(action.index);
      break;
    case "toggle":
      handlers.toggle(index);
      break;
    default: {
      const unhandled: never = action;
      void unhandled;
    }
  }
}
