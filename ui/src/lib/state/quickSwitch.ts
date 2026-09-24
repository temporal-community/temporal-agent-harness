/**
 * The Session Manager as a quick switcher: what a key does to the list, which row stays
 * highlighted as the filter narrows, and where focus goes when the drawer shuts.
 *
 * DOM-free for the reason `replayHotkeys.ts` is: these are the decisions, and the tests can
 * drive them without a browser. The component wires them to its own keydown.
 */

type ListKeyAction =
  | { kind: "move"; index: number }
  | { kind: "pick" }
  | { kind: "close" };

export interface ListKeyEvent {
  key: string;
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  /** Enter mid-IME confirms the word being spelled, and must not also pick a row. */
  isComposing: boolean;
}

/**
 * `null` means the key is not the list's, so a search box keeps it — which is how `s` in the
 * box types an "s". Modified keys are left alone: Shift+↑ selects text and Cmd+↑ goes to the
 * start of the field.
 */
function listKeyAction(event: ListKeyEvent, index: number, count: number): ListKeyAction | null {
  if (event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return null;
  switch (event.key) {
    case "ArrowDown":
      return count > 0 ? { kind: "move", index: Math.min(index + 1, count - 1) } : null;
    case "ArrowUp":
      return count > 0 ? { kind: "move", index: Math.max(index - 1, 0) } : null;
    case "Enter":
      return count > 0 && index >= 0 ? { kind: "pick" } : null;
    case "Escape":
      return { kind: "close" };
    default:
      return null;
  }
}

export interface ListKeyHandlers {
  move(index: number): void;
  pick(index: number): void;
  close(): void;
}

/**
 * The list's keydown, whole. A key it acts on is `preventDefault`ed, which is also what keeps
 * Escape here from the window's own Escape: that handler returns on `defaultPrevented` before
 * it asks the binding table anything, so leaving full screen cannot ride on closing the drawer.
 */
export function handleListKey(
  event: ListKeyEvent & { preventDefault(): void },
  index: number,
  count: number,
  handlers: ListKeyHandlers
): void {
  const action = listKeyAction(event, index, count);
  if (!action) return;
  event.preventDefault();
  switch (action.kind) {
    case "move":
      handlers.move(action.index);
      break;
    case "pick":
      handlers.pick(index);
      break;
    case "close":
      handlers.close();
      break;
    default: {
      const unhandled: never = action;
      void unhandled;
    }
  }
}

/** The highlighted row if the filter still shows it, else the first match, else nothing. */
export function keptHighlight(visibleIds: readonly string[], highlightedId: string | null): string | null {
  return highlightedId != null && visibleIds.includes(highlightedId) ? highlightedId : (visibleIds[0] ?? null);
}

/**
 * Choosing a New session row. A no-worker row opens its own hint and starts nothing; any other
 * row clears whichever hint was open and starts a session.
 */
export function chooseAgentRow(key: string, blocked: boolean): { hint: string | null; start: boolean } {
  return blocked ? { hint: key, start: false } : { hint: null, start: true };
}

/**
 * What was focused before the drawer opened, if it is still somewhere focus can go back to.
 * Gone from the page, or inside the drawer that is closing, means the trigger instead.
 */
export function focusReturnTarget(
  previous: HTMLElement | null,
  drawer: Node | null,
  trigger: HTMLElement | null
): HTMLElement | null {
  if (previous?.isConnected && !drawer?.contains(previous)) return previous;
  return trigger;
}
