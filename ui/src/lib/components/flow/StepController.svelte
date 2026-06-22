<script lang="ts">
  import {
    AlertTriangle,
    ChevronDown,
    ChevronLeft,
    ChevronRight,
    CircleDollarSign,
    Pause,
    Play,
    Radio,
    RotateCcw
  } from "@lucide/svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import MetricStrip from "$lib/components/primitives/MetricStrip.svelte";
  import UsageLineChart from "$lib/components/flow/UsageLineChart.svelte";
  import ModelBreakdown from "$lib/components/flow/ModelBreakdown.svelte";
  import type { Metric } from "$lib/components/primitives/metrics";
  import {
    formatCost,
    formatTokens,
    type CostSummary,
    type UsageTimelinePoint
  } from "$lib/cost/pricing";
  import type { PlaybackSpeed } from "$lib/state/agentRun.svelte";
  import type { ReplayLogRow, ReplayMarker } from "$lib/state/replayLog";

  interface Props {
    viewIndex: number;
    total: number;
    playing: boolean;
    following: boolean;
    playbackSpeed: PlaybackSpeed;
    currentEvent: ReplayLogRow | null;
    usage: CostSummary;
    usageTimeline: UsageTimelinePoint[];
    turnMarkers: Array<{ index: number; turnNumber: number }>;
    anomalyMarkers: ReplayMarker[];
    onPlay: () => void;
    onPause: () => void;
    onStepBack: () => void;
    onStepForward: () => void;
    onSpeedChange: (speed: PlaybackSpeed) => void;
    onJumpToLive: () => void;
    onReset: () => void;
    onScrub: (index: number) => void;
    onPreviousMarker: () => void;
    onNextMarker: () => void;
  }

  let {
    viewIndex,
    total,
    playing,
    following,
    playbackSpeed,
    currentEvent,
    usage,
    usageTimeline,
    turnMarkers,
    anomalyMarkers,
    onPlay,
    onPause,
    onStepBack,
    onStepForward,
    onSpeedChange,
    onJumpToLive,
    onReset,
    onScrub,
    onPreviousMarker,
    onNextMarker
  }: Props = $props();

  const playbackSpeeds: PlaybackSpeed[] = [1, 2, 5, 10];
  const markerCount = $derived(anomalyMarkers.length);
  let usageExpanded = $state(false);

  const metrics: Metric[] = $derived([
    { label: "cost", value: formatCost(usage.estimatedCostUsd), tone: "cost" },
    { label: "total", value: formatTokens(usage.tokens.total), tone: "strong" },
    { label: "input", value: formatTokens(usage.tokens.input) },
    { label: "output", value: formatTokens(usage.tokens.output) },
    { label: "thought", value: formatTokens(usage.tokens.thought) },
    { label: "cached", value: formatTokens(usage.tokens.cached) }
  ]);

  const currentLabel = $derived(
    currentEvent
      ? `${currentEvent.label} · turn ${currentEvent.turnNumber} · ${viewIndex}/${total}`
      : `Replay start · ${viewIndex}/${total}`
  );

  const currentBody = $derived(currentEvent?.body ?? currentEvent?.status ?? "");

  function handleInput(event: Event): void {
    onScrub(Number((event.currentTarget as HTMLInputElement).value));
  }
</script>

<footer class="step-controller">
  <div class="replay-row">
    <div class="transport">
      <IconButton label="Reset replay" onclick={onReset}>
        <RotateCcw size={16} />
      </IconButton>
      <IconButton label="Previous event" onclick={onStepBack} disabled={viewIndex === 0}>
        <ChevronLeft size={18} />
      </IconButton>
      <IconButton label="Next event" onclick={onStepForward} disabled={viewIndex >= total}>
        <ChevronRight size={18} />
      </IconButton>
      {#if playing}
        <IconButton label="Pause replay" tone="primary" onclick={onPause}>
          <Pause size={18} />
        </IconButton>
      {:else}
        <IconButton label="Play replay" tone="primary" onclick={onPlay}>
          <Play size={18} />
        </IconButton>
      {/if}
      <div class="speed-control" aria-label="Playback speed">
        {#each playbackSpeeds as speed}
          <button
            class:active={playbackSpeed === speed}
            type="button"
            aria-pressed={playbackSpeed === speed}
            onclick={() => onSpeedChange(speed)}
          >
            {speed}x
          </button>
        {/each}
      </div>
      <IconButton label="Jump to live" tone="live" pressed={following} onclick={onJumpToLive}>
        <Radio size={16} />
      </IconButton>

      {#if markerCount > 0}
        <span class="transport-divider" aria-hidden="true"></span>
        <div class="marker-nav" title={`${markerCount} flagged events (errors, approvals, queued turns)`}>
          <IconButton label="Previous flagged event" onclick={onPreviousMarker}>
            <ChevronLeft size={16} />
          </IconButton>
          <span class="marker-icon" aria-hidden="true"><AlertTriangle size={14} /></span>
          <IconButton label="Next flagged event" onclick={onNextMarker}>
            <ChevronRight size={16} />
          </IconButton>
        </div>
      {/if}
    </div>

    <div class="scrub-area">
      <div class="scrub-meta">
        <span>{currentLabel}</span>
        <span>{turnMarkers.length} turns</span>
      </div>
      <div class="range-wrap">
        <input
          aria-label="Replay position"
          type="range"
          min="0"
          max={total}
          value={viewIndex}
          oninput={handleInput}
        />
        <div class="turn-ticks" aria-hidden="true">
          {#each turnMarkers as marker}
            <span style={`left: ${(marker.index / Math.max(total, 1)) * 100}%`} title={`turn ${marker.turnNumber}`}></span>
          {/each}
        </div>
        <div class="event-markers">
          {#each anomalyMarkers as marker}
            <button
              type="button"
              class={`event-marker ${marker.tone}`}
              style={`left: ${(marker.index / Math.max(total, 1)) * 100}%`}
              title={`${marker.label} · turn ${marker.turnNumber} — click to jump`}
              aria-label={`Jump to ${marker.label}, turn ${marker.turnNumber}`}
              onclick={() => onScrub(marker.index)}
            ></button>
          {/each}
        </div>
      </div>
    </div>
  </div>

  <div class={`current-event ${currentEvent?.tone ?? "neutral"}`}>
    <span class="event-kicker">Now</span>
    <strong>{currentEvent?.label ?? "Replay start"}</strong>
    {#if currentEvent}
      <span class="event-type">{currentEvent.event}</span>
    {/if}
    {#if currentBody}
      <span class="event-body">{currentBody}</span>
    {/if}
  </div>

  <section class="usage-section" class:expanded={usageExpanded}>
    <button
      class="usage-toggle"
      type="button"
      aria-expanded={usageExpanded}
      aria-controls="usage-details"
      onclick={() => (usageExpanded = !usageExpanded)}
    >
      <span class="usage-toggle-title">
        <CircleDollarSign size={15} />
        <span>Token / Cost</span>
      </span>
      <span class="usage-toggle-summary">
        <strong>{formatTokens(usage.tokens.total)} tok</strong>
        <span>{formatCost(usage.estimatedCostUsd)}</span>
      </span>
      <span class="usage-toggle-icon" aria-hidden="true">
        <ChevronDown size={15} />
      </span>
    </button>

    {#if usageExpanded}
      <div id="usage-details" class="usage-row">
        <div class="usage">
          <div class="usage-title">
            <CircleDollarSign size={15} />
            <span>Replay totals</span>
          </div>
          <MetricStrip {metrics} dense />
        </div>
        <ModelBreakdown {usage} />
        <UsageLineChart points={usageTimeline} {viewIndex} />
      </div>
    {/if}
  </section>
</footer>

<style>
  .step-controller {
    display: grid;
    grid-template-rows: auto auto;
    gap: 12px;
    padding: 12px 14px;
    border-top: 1px solid var(--border);
    background: color-mix(in srgb, var(--surface-1) 92%, black);
  }

  .replay-row {
    display: grid;
    grid-template-columns: auto minmax(240px, 1fr);
    gap: 14px;
    align-items: center;
    width: 100%;
  }

  .transport {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 7px;
  }

  .speed-control {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    height: 32px;
    padding: 2px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-0);
  }

  .transport-divider {
    width: 1px;
    height: 22px;
    background: var(--border);
  }

  .marker-nav {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  .marker-icon {
    display: inline-flex;
    color: var(--error);
  }

  .speed-control button {
    min-width: 31px;
    height: 26px;
    padding: 0 6px;
    border: 0;
    border-radius: 5px;
    color: var(--text-3);
    background: transparent;
    cursor: pointer;
    font: inherit;
    font-size: 11px;
    font-variant-numeric: tabular-nums;
  }

  .speed-control button.active {
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 14%, var(--surface-2));
  }

  .scrub-area {
    min-width: 0;
  }

  .scrub-meta {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 5px;
    color: var(--text-3);
    font-size: 11px;
    white-space: nowrap;
  }

  .range-wrap {
    position: relative;
    height: 28px;
  }

  input[type="range"] {
    width: 100%;
    margin: 0;
    accent-color: var(--accent);
  }

  .turn-ticks {
    position: absolute;
    left: 6px;
    right: 6px;
    top: 20px;
    height: 6px;
    pointer-events: none;
  }

  .turn-ticks span {
    position: absolute;
    width: 2px;
    height: 6px;
    border-radius: 2px;
    background: var(--queue);
  }

  .event-markers {
    position: absolute;
    left: 6px;
    right: 6px;
    top: 0;
    height: 9px;
    z-index: 2;
  }

  .event-marker {
    position: absolute;
    top: 0;
    padding: 0;
    width: 9px;
    height: 9px;
    border-radius: 999px;
    border: 1px solid var(--surface-0);
    transform: translateX(-50%);
    background: var(--text-3);
    cursor: pointer;
    transition: transform 100ms ease;
  }

  .event-marker:hover {
    transform: translateX(-50%) scale(1.35);
  }

  .event-marker.approval {
    background: var(--queue);
  }

  .event-marker.error {
    background: var(--error);
  }

  .event-marker.queue {
    background: var(--warning);
  }

  .current-event {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
    color: var(--text-2);
    font-size: 12px;
  }

  .current-event strong {
    color: var(--text-1);
    font-size: 12px;
    white-space: nowrap;
  }

  .event-kicker {
    color: var(--accent);
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
  }

  .event-type {
    color: var(--text-3);
    font-size: 11px;
    white-space: nowrap;
  }

  .event-body {
    min-width: 0;
    overflow: hidden;
    color: var(--text-2);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .current-event.error {
    border-color: color-mix(in srgb, var(--error) 45%, var(--border));
  }

  .current-event.approval,
  .current-event.queue {
    border-color: color-mix(in srgb, var(--queue) 40%, var(--border));
  }

  .usage-section {
    min-width: 0;
    display: grid;
    gap: 10px;
  }

  .usage-toggle {
    min-width: 0;
    min-height: 36px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
    color: var(--text-2);
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .usage-toggle:hover,
  .usage-toggle:focus-visible {
    border-color: var(--border-strong);
    color: var(--text-1);
    outline: 0;
  }

  .usage-toggle-title,
  .usage-toggle-summary {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    white-space: nowrap;
  }

  .usage-toggle-title {
    color: var(--text-1);
    font-size: 12px;
    font-weight: 700;
  }

  .usage-toggle-summary {
    justify-self: end;
    overflow: hidden;
    color: var(--text-3);
    font-size: 12px;
  }

  .usage-toggle-summary strong {
    color: var(--text-1);
    font-weight: 700;
  }

  .usage-toggle-icon {
    display: inline-flex;
    color: var(--text-3);
    transition: transform 140ms ease;
  }

  .usage-section.expanded .usage-toggle-icon {
    transform: rotate(180deg);
  }

  .usage-row {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(300px, 0.85fr) minmax(220px, 0.7fr) minmax(320px, 1fr);
    gap: 12px;
    align-items: start;
  }

  .usage {
    min-width: 0;
    display: grid;
    align-content: start;
    gap: 8px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
  }

  .usage-title {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-2);
    font-size: 12px;
    white-space: nowrap;
  }

  @media (max-width: 1120px) {
    .replay-row,
    .usage-row {
      grid-template-columns: 1fr;
    }

    .transport {
      justify-content: flex-start;
    }
  }
</style>
