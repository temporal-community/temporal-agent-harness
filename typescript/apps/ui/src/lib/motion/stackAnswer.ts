import { prefersReducedMotion } from "svelte/motion";

/**
 * Counts the front rows answered, so a stack can key its front banner on it: the count moves when
 * the row shown in front leaves the queue, never when paging moves past one that stays.
 */
export function answeredFronts<K>() {
  let shown: K | null = null;
  let answered = 0;
  return (front: K | null, keys: K[]): number => {
    if (shown !== null && !keys.includes(shown)) answered += 1;
    shown = front;
    return answered;
  };
}

/**
 * An `out:` transition for an answered front banner. Its `is-answered` class drops it below the stack
 * on the stack's own CSS transition, in the same pass the next banner mounts from the plate
 * it was; this only holds the node for `--duration-settle`, unclickable as it goes.
 *
 * A banner wanted again while it drops keeps this same node: Svelte aborts the drop, lifts `inert`,
 * and ticks it to 1. That is the one tick where it is not inert, and it takes the class back; a
 * class left on would keep it out of flow and below the stack.
 */
export function stackAnswer(node: HTMLElement) {
  node.classList.add("is-answered");
  node.inert = true;
  if (prefersReducedMotion.current) return {};
  return {
    duration: parseFloat(getComputedStyle(node).getPropertyValue("--duration-settle")) || 350,
    tick: (t: number) => node.classList.toggle("is-answered", t < 1 || node.inert)
  };
}
