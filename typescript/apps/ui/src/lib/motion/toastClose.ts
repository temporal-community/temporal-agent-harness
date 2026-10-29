import { prefersReducedMotion } from "svelte/motion";

/**
 * An `out:` transition that closes a toast by taking its `is-open` class, on `--duration-pane`,
 * unclickable as it goes.
 *
 * A toast wanted again while it closes keeps this same node: Svelte aborts the close, lifts
 * `inert`, and ticks it to 1. That is the one tick where it is not inert, and it opens it again;
 * a class taken outside a tick would stay off, and the toast would be back but invisible.
 */
export function toastClose(node: HTMLElement) {
  node.inert = true;
  if (prefersReducedMotion.current) return {};
  return {
    duration: parseFloat(getComputedStyle(node).getPropertyValue("--duration-pane")) || 220,
    tick: (t: number) => node.classList.toggle("is-open", t === 1 && !node.inert)
  };
}
