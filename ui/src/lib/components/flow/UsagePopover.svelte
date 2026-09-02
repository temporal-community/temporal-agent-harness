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
    /**
     * The run's figures are unknown rather than zero, so show none of them.
     *
     * Every total below is a sum over the frames this console holds, and there
     * are runs it holds none of: a finished run whose event stream Temporal
     * cannot replay (`run.runUnmeasured`). An empty sum formatted as
     * `0 tok $0.0000` reports a measurement that was never taken, beside runs
     * whose zeros are real.
     */
    unmeasured?: boolean;
  }

  let { usage, usageTimeline, viewIndex, unmeasured = false }: Props = $props();

  /* Ids must be unique per instance; the popover can appear in several panes. */
  const instanceId = `usage-popover-${nextInstance()}`;

  let open = $state(false);
  let rootElement = $state<HTMLDivElement | null>(null);

  /* A dash says a figure is absent; it does not say why, and a reader who has
     only ever seen numbers here will read one as a broken panel. The full
     sentence is on the session banner already (`connectionError`), so this is
     the short form: what the dashes mean, and that nothing was lost. */
  const unmeasuredNote =
    "Unknown, not zero: this run finished and Temporal cannot replay its event stream, " +
    "so this console read none of the model calls it made. The run's own history is " +
    "intact in Temporal.";

  /* Two reasons a figure can be absent, and the reader gets ONE hedge. Being
     unmeasured is the wider claim — no events were read, so which of this run's
     models we hold prices for is moot — so it supersedes the unpriced note
     rather than stacking a second explanation beside it. */
  const costNote = $derived(
    unmeasured ? unmeasuredNote : (unpricedNote(unpricedModels(usage)) ?? undefined)
  );

  /** One figure, or the em dash that stands in for every figure of an unmeasured run. */
  function figure<T>(value: T, format: (value: T) => string): string {
    return unmeasured ? "—" : format(value);
  }

  /* What the run cost and how many tokens it took, which is the whole reading
     for most openings of this panel. */
  const headline: Metric[] = $derived([
    {
      label: "cost",
      value: figure(usage.estimatedCostUsd, formatCost),
      /* Tones are affirmative — cost is drawn in --success, total in --accent —
         and a dash has nothing to affirm. */
      tone: unmeasured ? "neutral" : "cost",
      /* The panel says it in full below, so the hover would be the same
         sentence twice in one box. */
      note: unmeasured ? undefined : costNote
    },
    {
      label: "total",
      value: figure(usage.tokens.total, formatTokens),
      tone: unmeasured ? "neutral" : "strong"
    }
  ]);

  /* Split off from the total rather than listed beside it, because these four
     OVERLAP each other and so cannot be added up — cached is a slice of input,
     and for most producers thought is a slice of output. Presented as a fifth
     peer of the total they read as addends, the sum comes to more than the
     total, and the total looks broken. It is not: it is the provider's own
     figure, and check-usage-totals.mjs pins it against exactly the arithmetic
     this separation is here to stop a reader attempting. */
  const breakdown: Metric[] = $derived([
    { label: "input", value: figure(usage.tokens.input, formatTokens) },
    { label: "output", value: figure(usage.tokens.output, formatTokens) },
    { label: "thought", value: figure(usage.tokens.thought, formatTokens) },
    { label: "cached", value: figure(usage.tokens.cached, formatTokens) }
  ]);

  const breakdownTip =
    "Each is part of the total, and they overlap: cached tokens are already inside " +
    "input, and reasoning tokens usually inside output. The total is the provider's " +
    "own figure, not the sum of these rows.";

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
    tone={unmeasured ? "neutral" : "success"}
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
    <span class="usage-chip-tokens">{figure(usage.tokens.total, formatTokens)}</span>
    <span class="usage-chip-cost" data-tip={costNote}
      >{figure(usage.estimatedCostUsd, formatCost)}</span>
  </Chip>

  {#if open}
    <div id={instanceId} class="usage-popover" role="dialog" aria-label="Token and cost details">
      <header class="usage-popover-head">
        <span>Token / Cost</span>
        <span class="usage-popover-summary">at cursor</span>
      </header>
      <div class="usage-popover-body">
        <div class="usage-metrics">
          <MetricStrip metrics={headline} dense />
          <p class="kicker usage-breakdown-label" data-tip={breakdownTip} data-tip-align="start">
            overlapping parts
          </p>
          <MetricStrip metrics={breakdown} dense />
        </div>
        <!-- A per-model split of nothing and a chart of a flat zero line are not
             hedged readings, they are empty boxes; the sentence that explains the
             dashes is the only thing this half of the panel has to say. -->
        {#if unmeasured}
          <p class="usage-unmeasured">{unmeasuredNote}</p>
        {:else}
          <ModelBreakdown {usage} />
          <UsageLineChart points={usageTimeline} {viewIndex} />
        {/if}
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

  /* Sits between the two strips and does the separating: a rule the eye stops at
     before it reaches figures it must not add. Dotted underline because the rest
     of the sentence is on hover. */
  .usage-breakdown-label {
    margin: 0;
    padding-top: var(--gutter-tight);
    border-top: 1px solid var(--border);
    text-decoration: underline dotted var(--text-3);
    text-underline-offset: 3px;
    cursor: help;
  }

  /* Takes the two tracks the model split and the chart have vacated, so the
     panel is one box of dashes and one sentence rather than a reading pushed
     into the left third of an otherwise empty 720px. */
  .usage-unmeasured {
    grid-column: 2 / -1;
    margin: 0;
    padding: var(--gutter);
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.4;
  }

  @media (max-width: 760px) {
    .usage-popover-body {
      grid-template-columns: 1fr;
    }

    /* One track, so spanning from the second would invent an implicit column. */
    .usage-unmeasured {
      grid-column: 1;
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
