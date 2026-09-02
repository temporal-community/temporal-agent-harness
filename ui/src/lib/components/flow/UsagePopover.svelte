<script lang="ts" module>
  let instanceCount = 0;

  function nextInstance(): number {
    return ++instanceCount;
  }
</script>

<script lang="ts">
  import { CircleDollarSign } from "@lucide/svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import MetricStrip from "$lib/components/primitives/MetricStrip.svelte";
  import UsageLineChart from "$lib/components/flow/UsageLineChart.svelte";
  import ModelBreakdown from "$lib/components/flow/ModelBreakdown.svelte";
  import type { Metric } from "$lib/components/primitives/metrics";
  import {
    formatCost,
    formatTokens,
    unpricedModels,
    unpricedNote,
    type CostSummary,
    type UsageTimelinePoint
  } from "$lib/cost/pricing";

  /**
   * The run's tokens and cost, as one chip in the transport that opens onto the
   * whole reading.
   *
   * A chip rather than a section of the footer: the transport is a row of
   * controls the hand sweeps across, and a panel that is always open costs it
   * three rows of height for figures nobody is reading most of the time.
   */
  interface Props {
    usage: CostSummary;
    usageTimeline: UsageTimelinePoint[];
    viewIndex: number;
  }

  let { usage, usageTimeline, viewIndex }: Props = $props();

  /* Ids must be unique per instance; the popover can appear in several panes. */
  const instanceId = `usage-popover-${nextInstance()}`;

  let open = $state(false);
  let rootElement = $state<HTMLDivElement | null>(null);

  const costNote = $derived(unpricedNote(unpricedModels(usage)) ?? undefined);

  const metrics: Metric[] = $derived([
    {
      label: "cost",
      value: formatCost(usage.estimatedCostUsd),
      tone: "cost",
      note: costNote
    },
    { label: "total", value: formatTokens(usage.tokens.total), tone: "strong" },
    { label: "input", value: formatTokens(usage.tokens.input) },
    { label: "output", value: formatTokens(usage.tokens.output) },
    { label: "thought", value: formatTokens(usage.tokens.thought) },
    { label: "cached", value: formatTokens(usage.tokens.cached) }
  ]);

  function toggle(): void {
    open = !open;
  }

  function close(): void {
    open = false;
  }

  /* ponytail: ceiling = Escape and outside-click are handled inline here, one
     copy for one popover. This repo has no shared dismissable behaviour yet, so
     a second dismissable surface would copy these two handlers rather than share
     them. Upgrade path = lift both into a shared attachment once there is a
     second caller to justify it. */
  function onWindowPointerDown(event: PointerEvent): void {
    if (!open || !rootElement) return;
    if (event.target instanceof Node && rootElement.contains(event.target)) return;
    // Footer counts as "inside" so measures can stay open while scrubbing.
    if (event.target instanceof Element && event.target.closest?.(".step-controller")) {
      return;
    }
    close();
  }

  function onWindowKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape" && open) close();
  }
</script>

<svelte:window onpointerdown={onWindowPointerDown} onkeydown={onWindowKeydown} />

<div class="usage-anchor" bind:this={rootElement}>
  <Chip
    tone="success"
    active={open}
    class="usage-chip"
    aria-expanded={open}
    aria-haspopup="dialog"
    aria-controls={instanceId}
    onclick={toggle}
  >
    {#snippet lead()}
      <CircleDollarSign size={12} />
    {/snippet}
    <span class="usage-chip-tokens">{formatTokens(usage.tokens.total)}</span>
    <span class="usage-chip-cost" data-tip={costNote}
      >{formatCost(usage.estimatedCostUsd)}</span>
  </Chip>

  {#if open}
    <div id={instanceId} class="usage-popover" role="dialog" aria-label="Token and cost details">
      <header class="usage-popover-head">
        <span>Token / Cost</span>
        <span class="usage-popover-summary">at cursor</span>
      </header>
      <div class="usage-popover-body">
        <div class="usage-metrics">
          <MetricStrip {metrics} dense />
        </div>
        <ModelBreakdown {usage} />
        <UsageLineChart points={usageTimeline} {viewIndex} />
      </div>
    </div>
  {/if}
</div>

<style>
  .usage-anchor {
    position: relative;
    display: inline-flex;
  }

  /* Only the numerals need saying; the chip shape comes from Chip. */
  :global(.usage-chip) {
    font-variant-numeric: tabular-nums;
  }

  /* Room for the widest reading each figure can reach, for the reason the
     position readout beside it reserves its own: the scrub lane is the flex item
     that pays for a readout that grows, and a lane that changes width moves every
     mark a hand is reaching for. Both are right-aligned, so the digits grow into
     the room rather than shunting their neighbour. */
  .usage-chip-tokens {
    display: inline-block;
    min-width: 9ch;
    text-align: right;
  }

  .usage-chip-cost {
    display: inline-block;
    min-width: 7ch;
    color: var(--text-2);
    text-align: right;
  }

  /* Opens up, out of the transport row it sits in, and from the corner it is
     pinned to — the same path the event readout's card beside it takes. */
  .usage-popover {
    position: absolute;
    right: 0;
    bottom: calc(100% + var(--gap-md));
    z-index: 40;
    width: min(720px, calc(100vw - 24px));
    display: grid;
    gap: var(--gutter);
    padding: var(--gutter);
    border: 1px solid var(--border-strong);
    background: var(--surface-1);
    box-shadow: var(--shadow-modal);
    transform-origin: bottom right;
    opacity: 1;
    transform: none;
    transition:
      opacity var(--duration-fast) var(--ease-out),
      transform var(--duration-fast) var(--ease-out);
  }

  @starting-style {
    .usage-popover {
      opacity: 0;
      transform: scale(0.97) translateY(3px);
    }
  }

  .usage-popover-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    color: var(--text-1);
    font-family: var(--font-mono);
    font-size: var(--label-size);
    font-weight: var(--label-weight);
    letter-spacing: var(--label-tracking);
    text-transform: uppercase;
  }

  .usage-popover-summary {
    flex: 1;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-weight: 650;
    font-variant-numeric: tabular-nums;
  }

  .usage-popover-body {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(180px, 0.85fr) minmax(170px, 0.7fr) minmax(200px, 1fr);
    gap: var(--gutter);
    /* Equal heights: three boxes of three different heights read as three
       unrelated things rather than one reading of the same number. */
    align-items: stretch;
  }

  .usage-metrics {
    min-width: 0;
    display: grid;
    align-content: start;
    gap: var(--gutter-tight);
    padding: var(--gutter);
    border: 1px solid var(--border);
    background: var(--surface-2);
  }

  @media (max-width: 760px) {
    .usage-popover-body {
      grid-template-columns: 1fr;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .usage-popover {
      transition: opacity var(--duration-fast) var(--ease-out);
    }

    @starting-style {
      .usage-popover {
        opacity: 0;
        transform: none;
      }
    }
  }
</style>
