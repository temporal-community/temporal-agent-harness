<script lang="ts">
  import {
    ChevronLeft,
    ChevronRight,
    Pause,
    Play,
    RotateCcw,
    SkipForward
  } from "@lucide/svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import UsagePopover from "$lib/components/flow/UsagePopover.svelte";
  import type { CostSummary, UsageTimelinePoint } from "$lib/cost/pricing";
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
    onScrub
  }: Props = $props();

  const playbackSpeeds: PlaybackSpeed[] = [1, 2, 5, 10];

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
      <IconButton label="Jump to latest step" tone="follow" pressed={following} onclick={onJumpToLive}>
        <SkipForward size={16} />
      </IconButton>
      <UsagePopover {usage} {usageTimeline} {viewIndex} />
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
    <span class="event-kicker kicker">Now</span>
    <strong>{currentEvent?.label ?? "Replay start"}</strong>
    {#if currentEvent}
      <span class="event-type">{currentEvent.event}</span>
    {/if}
    {#if currentBody}
      <span class="event-body">{currentBody}</span>
    {/if}
  </div>

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
    border-radius: var(--radius-md);
    background: var(--surface-0);
  }

  .speed-control button {
    min-width: 31px;
    height: 26px;
    padding: 0 6px;
    border: 0;
    border-radius: var(--radius-sm);
    color: var(--text-3);
    background: transparent;
    cursor: pointer;
    font: inherit;
    font-size: var(--font-sm);
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
    font-size: var(--font-sm);
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
    cursor: grab;
  }

  input[type="range"]:active {
    cursor: grabbing;
  }

  input[type="range"]::-webkit-slider-thumb {
    cursor: grab;
  }

  input[type="range"]:active::-webkit-slider-thumb {
    cursor: grabbing;
  }

  input[type="range"]::-moz-range-thumb {
    cursor: grab;
  }

  input[type="range"]:active::-moz-range-thumb {
    cursor: grabbing;
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
    border-radius: var(--radius-2xs);
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
    border-radius: var(--radius-chip);
    border: 1px solid var(--surface-0);
    transform: translateX(-50%);
    background: var(--text-3);
    cursor: pointer;
    transition: transform var(--duration-fast) var(--ease-ui);
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
    border-radius: var(--radius-md);
    background: var(--surface-2);
    color: var(--text-2);
    font-size: var(--font-md);
  }

  .current-event strong {
    color: var(--text-1);
    font-size: var(--font-md);
    white-space: nowrap;
  }

  .event-kicker {
    color: var(--accent);
  }

  .event-type {
    color: var(--text-3);
    font-size: var(--font-sm);
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

  @media (max-width: 1120px) {
    .replay-row {
      grid-template-columns: 1fr;
    }

    .transport {
      justify-content: flex-start;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .event-marker {
      transition: none;
    }

    .event-marker:hover {
      transform: translateX(-50%);
    }
  }
</style>
