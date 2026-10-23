<script lang="ts">
  import { ChevronDown, ChevronUp } from "@lucide/svelte";
  import { untrack } from "svelte";
  import { quintOut } from "svelte/easing";
  import { prefersReducedMotion } from "svelte/motion";
  import IconButton from "$lib/components/primitives/IconButton.svelte";

  interface Props {
    /** Zero-based. */
    index: number;
    count: number;
    /** "approval": "Previous approval", and "Approval 1 of 3" for screen readers. */
    noun: string;
    onstep: (by: 1 | -1) => void;
    /** Announce the position itself, for a pager the queue's own live region does not cover. */
    live?: boolean;
    /** Show the position in words, "Question 2 of 3", rather than "2/3". */
    spelled?: boolean;
  }

  let { index, count, noun, onstep, live = false, spelled = false }: Props = $props();

  const title = $derived(noun.charAt(0).toUpperCase() + noun.slice(1));
  let previous = untrack(() => [index, count]);
  /* Forward, or the same place in a shorter queue, rolls up; back rolls down. */
  const rising = $derived.by(() => {
    const [i, n] = previous;
    previous = [index, count];
    return index > i! || (index === i && count < n!);
  });

  function roll(node: HTMLElement, { into }: { into: boolean }) {
    if (prefersReducedMotion.current) return { duration: 0 };
    const from = (into ? 1 : -1) * (rising ? 1 : -1);
    return {
      duration: parseFloat(getComputedStyle(node).getPropertyValue("--duration-settle")),
      easing: quintOut,
      css: (_t: number, u: number) => `transform: translateY(${u * from * 100}%); opacity: ${1 - u};`
    };
  }
</script>

<!-- Digits keyed by place value, so 9 → 10 rolls the ones and adds a tens digit. -->
{#snippet digits(value: number)}
  {#each [...String(value)] as digit, i (String(value).length - i)}
    <span class="slot">
      {#key digit}
        <span in:roll={{ into: true }} out:roll={{ into: false }}>{digit}</span>
      {/key}
    </span>
  {/each}
{/snippet}

<div class="pager">
  <IconButton label={`Previous ${noun}`} data-pager="prev" disabled={index === 0} onclick={() => onstep(-1)}>
    <ChevronUp size={14} />
  </IconButton>
  <span class={["count", { spelled }]}>
    <span class="sr-only" aria-live={live ? "polite" : undefined}>{title} {index + 1} of {count}</span>
    <span aria-hidden="true">
      {#if spelled}{title}&nbsp;{@render digits(index + 1)}&nbsp;of&nbsp;{@render digits(count)}{:else}{@render digits(index + 1)}/{@render digits(count)}{/if}
    </span>
  </span>
  <IconButton label={`Next ${noun}`} data-pager="next" disabled={index >= count - 1} onclick={() => onstep(1)}>
    <ChevronDown size={14} />
  </IconButton>
</div>

<style>
  .pager {
    display: flex;
    gap: 4px;
    align-items: center;
  }

  .pager :global(.icon-button) {
    border-color: transparent;
    background: none;
  }

  /* LTR even in an RTL page: "1/3", not "3/1". */
  .count {
    display: inline-flex;
    direction: ltr;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    font-variant-numeric: tabular-nums;
  }

  /* Words read in the page's direction. */
  .count.spelled {
    direction: inherit;
  }

  .count > span[aria-hidden] {
    display: inline-flex;
  }

  .slot {
    display: inline-grid;
    overflow: clip;
  }

  .slot > * {
    grid-area: 1 / 1;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
</style>
