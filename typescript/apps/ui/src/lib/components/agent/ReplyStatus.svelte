<script lang="ts">
  import type { Snippet } from "svelte";
  import { formatReplyElapsed } from "$lib/state/replyStatus";

  interface Props {
    label: string;
    /** The turn's start, epoch seconds, from its own event. */
    startedAt: number | null;
    /** The newest event's time: where a replay cursor stands, and the live reading until the clock ticks. */
    now: number | null;
    /** At the live head the timer follows the wall clock; anywhere else it stays at `now`. */
    live?: boolean;
    /** Drawn over the loader in its cell: the caret of the turn header it sits in. */
    children?: Snippet;
  }

  let { label, startedAt, now, live = false, children }: Props = $props();

  let wall = $state<number | null>(null);
  $effect(() => {
    if (!live) return;
    const tick = () => (wall = Date.now() / 1000);
    tick();
    const timer = setInterval(tick, 100);
    return () => clearInterval(timer);
  });

  const elapsed = $derived(formatReplyElapsed(startedAt, (live ? wall : null) ?? now));
  /* A chevron wavefront: each cell lights one step after the cell before it, rows bending back. */
  const CELLS = Array.from({ length: 9 }, (_, i) => (i % 3) + Math.abs(Math.floor(i / 3) - 1));
</script>

<!-- Seen, not heard: the chat's own status region says what changed, once. -->
<span class="reply-status" aria-hidden="true" title={label}>
  <span class="trace-glyph">
    <span class="pixels">
      {#each CELLS as step, index (index)}
        <span style:--step={step}></span>
      {/each}
    </span>
    {@render children?.()}
  </span>
  <span class="label reply-shimmer">{label}</span>
  {#if elapsed}
    <span class="elapsed">{elapsed}</span>
  {/if}
</span>

<style>
  .reply-status {
    height: var(--control-height-xs);
    min-width: 0;
    max-width: 100%;
    display: inline-flex;
    align-items: center;
    gap: var(--gap-sm);
    font-size: var(--font-md);
    line-height: 1;
  }

  /* 11px, the visual weight of a 13px StateGlyph's ring, in the trace's own glyph cell. */
  .pixels {
    display: grid;
    grid-template-columns: repeat(3, 3px);
    grid-auto-rows: 3px;
    gap: 1px;
  }

  .pixels > span {
    background: var(--accent-soft);
    opacity: 0.15;
    animation: reply-pixel-on var(--duration-pixel-cycle) var(--ease-in-out) infinite;
    animation-delay: calc(var(--step) * var(--duration-pixel-step));
  }

  .label {
    min-width: 0;
    overflow: hidden;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
    /* A tool's name keeps its own direction, so it is cut at its end in either layout. */
    unicode-bidi: plaintext;
  }

  .elapsed {
    flex: none;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--figure-size);
    font-variant-numeric: tabular-nums;
  }

  @keyframes reply-pixel-on {
    0%,
    100% {
      opacity: 0.15;
    }
    35% {
      opacity: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .pixels > span {
      animation: none;
      opacity: 0.35;
    }
  }
</style>
