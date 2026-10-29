import type { Attachment } from "svelte/attachments";

/**
 * Where a key moves focus from row `from`: a row index, `null` for a key the list owns that
 * goes nowhere from here, or `undefined` for a key it leaves alone.
 */
export type MoveKey = (key: string, from: number) => number | null | undefined;

/** The feed's keys: PageDown and PageUp move a row at a time. */
export const pageKey: MoveKey = (key, from) => {
  if (key === "PageDown") return from + 1;
  if (key === "PageUp") return from - 1;
  return undefined;
};

/**
 * Focus and keyboard for a windowed list, whose rows (`[data-index]`, `tabindex="-1"`) mount
 * and unmount as it scrolls. Attach it to the list's scroller, which needs `tabindex="-1"`.
 *
 * A focused row scrolled out of the window leaves the DOM, and the browser drops focus on
 * <body>, so the next Tab starts again from the top of the page. When that happens the row
 * now drawn at the same index takes focus back, or the list itself does when none is.
 *
 * Given `scrollToIndex`, PageDown and PageUp move focus between rows as the ARIA feed pattern
 * has it, scrolling an undrawn row into the window first, so the keyboard reaches every row
 * and not only the ones drawn. A list with more keys (a tree's arrows) passes its own `moveKey`.
 */
export function windowedFocus(
  scrollToIndex?: (index: number) => void,
  count: () => number = () => 0,
  moveKey: MoveKey = pageKey
): Attachment<HTMLElement> {
  return (list) => {
    let held: { node: Element; index: number | null } | null = null;

    const rowAt = (index: number) => list.querySelector<HTMLElement>(`[data-index="${index}"]`);
    const indexOf = (node: Element) => {
      const row = node.closest("[data-index]");
      return row && row !== list && list.contains(row) ? Number(row.getAttribute("data-index")) : null;
    };

    const check = () => {
      if (!held) return;
      const active = document.activeElement;
      if (active && active !== document.body) {
        if (!list.contains(active)) held = null;
        return;
      }
      /* On <body> with the node still in the page, the reader moved focus away themselves. */
      if (held.node.isConnected) {
        held = null;
        return;
      }
      const row = held.index == null ? null : rowAt(held.index);
      (row ?? list).focus({ preventScroll: true });
    };

    const focusWhenDrawn = (index: number, tries = 10) => {
      const row = rowAt(index);
      if (row) row.focus({ preventScroll: true });
      else if (tries > 0) requestAnimationFrame(() => focusWhenDrawn(index, tries - 1));
    };

    /* The first row whose foot is below the list's top: where a reader on the list starts. */
    const firstInView = () => {
      const top = list.getBoundingClientRect().top;
      const drawn = [...list.querySelectorAll<HTMLElement>("[data-index]")];
      const row = drawn.find((node) => node.getBoundingClientRect().bottom > top) ?? drawn[0];
      return row ? Number(row.getAttribute("data-index")) : 0;
    };

    const onFocusIn = (event: FocusEvent) => {
      const node = event.target as Element;
      held = { node, index: indexOf(node) };
    };
    const onFocusOut = () => queueMicrotask(check);
    /* Paging inside a field, or inside a scroller of the row's own, is not the list's to take. */
    const pagesItself = (target: EventTarget | null) => {
      for (let node = target as HTMLElement | null; node && node !== list; node = node.parentElement) {
        if (node.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName)) return true;
        if (node.scrollHeight > node.clientHeight && /auto|scroll/.test(getComputedStyle(node).overflowY)) return true;
      }
      return false;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      /* A chord is someone else's shortcut, such as the replay transport's. */
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (!scrollToIndex || moveKey(event.key, 0) === undefined) return;
      if (pagesItself(event.target)) return;
      const total = count();
      if (total === 0) return;
      const from = held?.index;
      const target = from == null ? firstInView() : moveKey(event.key, from);
      if (target == null) return;
      const index = Math.min(total - 1, Math.max(0, target));
      event.preventDefault();
      scrollToIndex(index);
      focusWhenDrawn(index);
    };

    /* Not every browser sends focusout for a node removed while focused; the removal is certain. */
    const observer = new MutationObserver(check);
    observer.observe(list, { childList: true, subtree: true });
    list.addEventListener("focusin", onFocusIn);
    list.addEventListener("focusout", onFocusOut);
    list.addEventListener("keydown", onKeyDown);
    return () => {
      observer.disconnect();
      list.removeEventListener("focusin", onFocusIn);
      list.removeEventListener("focusout", onFocusOut);
      list.removeEventListener("keydown", onKeyDown);
    };
  };
}
