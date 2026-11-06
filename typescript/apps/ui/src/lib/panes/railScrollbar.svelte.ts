/** The narrowest the thumb gets, so a very wide desk still leaves something to grab. */
const MIN_THUMB = 32;

/** The overlay bar's thumb, in px along the rail's visible width. Null when the rail does not scroll. */
export function railThumb(
  scrollLeft: number,
  clientWidth: number,
  scrollWidth: number
): { left: number; width: number } | null {
  const range = scrollWidth - clientWidth;
  if (!(clientWidth > 0) || range < 1) return null;
  const width = Math.min(clientWidth, Math.max(MIN_THUMB, Math.round((clientWidth * clientWidth) / scrollWidth)));
  const left = Math.round((Math.min(Math.max(scrollLeft, 0), range) / range) * (clientWidth - width));
  return { left, width };
}

/** Where the rail scrolls to with the thumb's left edge at `thumbLeft`. */
export function scrollForThumb(
  thumbLeft: number,
  clientWidth: number,
  scrollWidth: number,
  thumbWidth: number
): number {
  const travel = clientWidth - thumbWidth;
  const range = scrollWidth - clientWidth;
  if (travel <= 0 || range <= 0) return 0;
  return (Math.min(Math.max(thumbLeft, 0), travel) / travel) * range;
}

/**
 * Shown on activity, hidden after `idleMs` without any. Held open while the reader is
 * on the bar or dragging it, and counted down again once they let go.
 */
export class AutoHide {
  visible = $state(false);
  #timer: ReturnType<typeof setTimeout> | undefined;
  #held = false;

  constructor(readonly idleMs = 2000) {}

  poke(): void {
    this.visible = true;
    this.#countDown();
  }

  hold(held: boolean): void {
    this.#held = held;
    if (held) this.visible = true;
    this.#countDown();
  }

  dispose(): void {
    clearTimeout(this.#timer);
  }

  #countDown(): void {
    clearTimeout(this.#timer);
    if (this.#held) return;
    this.#timer = setTimeout(() => (this.visible = false), this.idleMs);
  }
}
