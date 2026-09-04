/**
 * Keep the row holding the playhead on screen, inside a pane's OWN scroller, and
 * stand down as soon as the reader scrolls it themselves.
 *
 * This replaces `element.scrollIntoView({ block: "nearest" })` in the two panes that
 * follow the playhead, and it exists because `scrollIntoView` scrolls every scrollable
 * ANCESTOR, not just the box the row lives in. `PaneRail`'s `.rail` is `overflow-x:
 * auto` on purpose — a desk wider than the window scrolls sideways — so a log row
 * coming into view slid the entire desk 121px sideways on every scrub tick, moving
 * panes the reader was working in. `block: "nearest"` does not help: it names the
 * block axis and says nothing about which boxes are allowed to move. Writing
 * `scrollTop` on one element cannot move anything above it, which is the whole point.
 *
 * Three other things it fixes, all of them the same shape as behaviour already here:
 *
 * - Jumped, never animated, exactly as `AgentStateNode` scrolls arriving text. A
 *   smooth scroll restarted every scrub tick never arrives: it measured at ten scroll
 *   events per tick, which is what "moving all fast and crazy" is. `scrollTop` is
 *   instant, so the seek-count/keyboard test that used to pick the easing has nothing
 *   left to decide and is gone from both callers.
 *
 * - One settle per frame. A pointer drag or a burst of `reply_delta` frames moves the
 *   playhead many times before the screen is painted; only the last one is worth
 *   scrolling to.
 *
 * - Manual action wins. `AgentStateNode` asks `scrollTop + clientHeight ===
 *   scrollHeight`; the equivalent question for a playhead follower is "is the playhead
 *   still on screen", so that is the test. It is recomputed on every scroll rather than
 *   latched, which means it also forgives Chrome's scroll anchoring: an adjustment that
 *   keeps the row where it was keeps the follow, and only a scroll that carries the
 *   playhead off screen hands over control.
 *
 * Nothing here touches focus. Following the playhead must never move the caret, and
 * the only way to be sure of that is to own no code that could.
 */
export interface ScrollFollower {
  /** Bring the element with this id into view, at most once per frame. */
  to(elementId: string): void;
  /** The scroller's own `onscroll`: a scroll this did not write may hand over control. */
  handleScroll(): void;
}

/** How far `scrollTop` may differ from what we wrote and still count as our own write. */
const OWN_WRITE_SLACK = 1;

/**
 * @param scroller Reads the scroll container — a getter, not the element, so the
 *   caller can hand over a `bind:this` target that is still null at setup.
 */
export function scrollFollower(scroller: () => HTMLElement | null): ScrollFollower {
  let following = true;
  let written = -1;
  let frame = 0;
  let target: string | null = null;

  function onScreen(row: HTMLElement, view: DOMRect): boolean {
    const rect = row.getBoundingClientRect();
    return rect.bottom > view.top && rect.top < view.bottom;
  }

  return {
    to(elementId: string): void {
      target = elementId;
      /* Coalesced by keeping the first frame rather than rescheduling: `target` is
         read inside the callback, so the pending frame already means "scroll to
         wherever the playhead has got to by the time we paint". */
      if (frame !== 0) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const box = scroller();
        const row = target == null ? null : document.getElementById(target);
        if (!box || !row || !following) return;

        const view = box.getBoundingClientRect();
        const rect = row.getBoundingClientRect();
        /* "Nearest", by hand: a row already inside the box is not moved at all, and a
           row taller than the box aligns its top rather than scrolling past it. */
        const delta =
          rect.top < view.top
            ? rect.top - view.top
            : rect.bottom > view.bottom
              ? Math.min(rect.top - view.top, rect.bottom - view.bottom)
              : 0;
        if (delta === 0) return;
        box.scrollTop += delta;
        written = box.scrollTop;
      });
    },

    handleScroll(): void {
      const box = scroller();
      if (!box) return;
      if (Math.abs(box.scrollTop - written) <= OWN_WRITE_SLACK) return;
      const row = target == null ? null : document.getElementById(target);
      following = row == null || onScreen(row, box.getBoundingClientRect());
    }
  };
}
