<script lang="ts">
  import Chip from "$lib/components/primitives/Chip.svelte";
  import { formatTokens, type ContextWindowUsage } from "$lib/cost/pricing";

  /**
   * How much of the model's context window the conversation fills: a chip with the percent,
   * and on hover or focus a card with the counts over a bar. The card is the `data-tip`
   * bubble's surface, delay and entrance, drawn as an element because a pseudo-element
   * cannot hold a bar.
   */
  let { usage }: { usage: ContextWindowUsage } = $props();

  const id = $props.id();
  const percent = $derived(usage.used / usage.limit);
  const nearlyFull = $derived(percent >= 0.9);
  const percentFormat = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });
  /* A filled window is never "0%": below the last digit it reads as under it. */
  const percentLabel = $derived(
    percent > 0 && percent < 0.001 ? `<${percentFormat.format(0.001)}` : percentFormat.format(percent)
  );
</script>

<span class="context" class:nearly-full={nearlyFull}>
  <Chip fill="bare" tone={nearlyFull ? "error" : "neutral"} toned={nearlyFull} tabindex={0} aria-describedby={id}>
    {percentLabel} context
  </Chip>
  <span class="context-card" {id} role="tooltip">
    <span class="context-head">
      <span>Context</span>
      <span class="context-counts">{formatTokens(usage.used)} / {formatTokens(usage.limit)}</span>
    </span>
    <progress value={Math.min(usage.used, usage.limit)} max={usage.limit} aria-label="Context used"></progress>
  </span>
</span>

<style>
  .context {
    position: relative;
    display: inline-flex;
  }

  /* Hidden is gone, not transparent, for the reason the tip's bubble is (app.css). */
  .context-card {
    position: absolute;
    z-index: 40;
    bottom: calc(100% + 8px);
    inset-inline-end: 0;
    display: none;
    flex-direction: column;
    gap: 6px;
    box-sizing: border-box;
    width: max-content;
    min-width: 180px;
    max-width: min(320px, 60vw);
    padding: 8px;
    border: 1px solid var(--border-strong);
    background: var(--surface-3);
    box-shadow: var(--shadow-floating);
    color: var(--text-1);
    font-family: var(--font-sans);
    font-size: var(--font-sm);
    font-weight: 500;
    line-height: 1.4;
    pointer-events: none;
    --tip-rise: var(--motion-rise);
  }

  @media (prefers-reduced-motion: reduce) {
    .context-card {
      --tip-rise: 0px;
    }
  }

  .context:hover .context-card,
  .context:has(:global(:focus-visible)) .context-card {
    display: flex;
    animation: tip-in var(--duration-fast) var(--ease-ui) var(--tip-delay) both;
  }

  .context:has(:global(:focus-visible)) .context-card {
    animation-delay: 0s;
  }

  .context-head {
    display: flex;
    justify-content: space-between;
    gap: 16px;
  }

  .context-counts {
    color: var(--text-3);
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }

  progress {
    --fill: var(--accent);
    width: 100%;
    height: 4px;
    border: 0;
    appearance: none;
    background: var(--surface-1);
  }

  .nearly-full progress {
    --fill: var(--error);
  }

  progress::-webkit-progress-bar {
    background: var(--surface-1);
  }

  progress::-webkit-progress-value {
    background: var(--fill);
  }

  progress::-moz-progress-bar {
    background: var(--fill);
  }
</style>
