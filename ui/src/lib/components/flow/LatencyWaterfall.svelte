<script lang="ts">
  import { Cpu, ShieldCheck, Wrench } from "@lucide/svelte";
  import { formatDuration } from "$lib/state/replayLog";
  import {
    aggregateSpans,
    type SpanKind,
    type StepTimeline,
    type TimelineSpan
  } from "$lib/state/stepTimeline";

  interface Props {
    timeline: StepTimeline;
    viewIndex: number;
    onScrub: (index: number) => void;
  }

  let { timeline, viewIndex, onScrub }: Props = $props();

  const aggregates = $derived(aggregateSpans(timeline));
  const totalSpanSeconds = $derived(
    aggregates.reduce((sum, agg) => sum + agg.totalSeconds, 0)
  );
  const subagentTurnCount = $derived(
    timeline.turns.reduce((sum, turn) => sum + turn.subagentTurns.length, 0)
  );

  function pct(span: TimelineSpan, turnStart: number): { left: number; width: number } {
    const scale = Math.max(timeline.maxTurnDuration, 1);
    const left = Math.min(99.5, Math.max(0, ((span.startTs - turnStart) / scale) * 100));
    const rawWidth = (span.durationSeconds / scale) * 100;
    return {
      left,
      width: Math.min(100 - left, Math.max(rawWidth, 1.5))
    };
  }

  function trackHeight(laneCount: number): number {
    return 6 + laneCount * 24;
  }

  function laneTop(span: TimelineSpan): number {
    return 3 + span.lane * 24;
  }

  function spanState(span: TimelineSpan): "past" | "active" | "future" {
    if (viewIndex < span.startIndex) return "future";
    if (viewIndex >= span.startIndex && viewIndex <= span.endIndex) return "active";
    return "past";
  }

  function barClass(span: TimelineSpan): string {
    const tone = span.tone === "error" ? "error" : span.kind;
    return ["bar", tone, spanState(span), span.ongoing ? "ongoing" : ""]
      .filter(Boolean)
      .join(" ");
  }

  function spanTitle(span: TimelineSpan): string {
    const detail = span.detail ? ` · ${span.detail}` : "";
    return `${span.label} · ${formatDuration(span.durationSeconds)}${detail}`;
  }

  const kindLabel: Record<SpanKind, string> = {
    model: "model",
    tool: "tool",
    approval: "approval"
  };
</script>

<section class="waterfall" aria-label="Latency waterfall">
  <header class="waterfall-head">
    <div class="title">
      <h2>Latency waterfall</h2>
      <p>
        Per-step wall-clock across {timeline.turns.length} parent turns
        {#if subagentTurnCount > 0}
          · {subagentTurnCount} nested subagent turns
        {/if}
        · bars share one time scale
      </p>
    </div>
    <div class="rollup" aria-label="Time by step kind">
      {#each aggregates as agg}
        <div class={`roll ${agg.kind}`}>
          <span class="roll-icon" aria-hidden="true">
            {#if agg.kind === "model"}
              <Cpu size={14} />
            {:else if agg.kind === "tool"}
              <Wrench size={14} />
            {:else}
              <ShieldCheck size={14} />
            {/if}
          </span>
          <span class="roll-text">
            <strong>{formatDuration(agg.totalSeconds)}</strong>
            <small>
              {kindLabel[agg.kind]} · {agg.count}×
              {#if totalSpanSeconds > 0}
                · {Math.round((agg.totalSeconds / totalSpanSeconds) * 100)}%
              {/if}
            </small>
          </span>
        </div>
      {/each}
    </div>
  </header>

  <div class="turns">
    {#if timeline.turns.length === 0}
      <p class="empty">Step through the stream to chart per-step latency.</p>
    {:else}
      {#each timeline.turns as turn}
        <article class="turn-row">
          <div class="turn-label">
            <p class="turn-no">Turn {turn.turnNumber}</p>
            <p class="turn-dur">{formatDuration(turn.durationSeconds)}</p>
          </div>
          <div class="turn-body">
            <div class="track parent-track" style={`height: ${trackHeight(turn.laneCount)}px`}>
              {#each turn.spans as span}
                {@const box = pct(span, turn.startTs)}
                <button
                  class={barClass(span)}
                  style={`left: ${box.left}%; width: ${box.width}%; top: ${laneTop(span)}px`}
                  title={spanTitle(span)}
                  onclick={() => onScrub(span.startIndex)}
                >
                  <span class="bar-text">{span.label} · {formatDuration(span.durationSeconds)}</span>
                </button>
              {/each}
              {#if turn.spans.length === 0}
                <span class="track-empty">no measured parent steps</span>
              {/if}
            </div>

            {#if turn.subagentTurns.length > 0}
              <div class="subagent-stack" aria-label={`Subagent latency for turn ${turn.turnNumber}`}>
                {#each turn.subagentTurns as subagent}
                  <div class="subagent-row">
                    <div class="subagent-label">
                      <span>Subagent</span>
                      <strong title={subagent.label}>{subagent.label}</strong>
                      <small>
                        turn {subagent.turnNumber} · {formatDuration(subagent.durationSeconds)}
                      </small>
                    </div>
                    <div class="track subagent-track" style={`height: ${trackHeight(subagent.laneCount)}px`}>
                      {#each subagent.spans as span}
                        {@const box = pct(span, turn.startTs)}
                        <button
                          class={barClass(span)}
                          style={`left: ${box.left}%; width: ${box.width}%; top: ${laneTop(span)}px`}
                          title={`${subagent.label} · ${spanTitle(span)}`}
                          onclick={() => onScrub(span.startIndex)}
                        >
                          <span class="bar-text">
                            {span.label} · {formatDuration(span.durationSeconds)}
                          </span>
                        </button>
                      {/each}
                      {#if subagent.spans.length === 0}
                        <span class="track-empty">no measured subagent steps</span>
                      {/if}
                    </div>
                  </div>
                {/each}
              </div>
            {/if}
          </div>
        </article>
      {/each}
    {/if}
  </div>
</section>

<style>
  .waterfall {
    width: 100%;
    height: 100%;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    background: var(--surface-0);
  }

  .waterfall-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 14px;
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-1);
  }

  .title h2 {
    margin: 0;
    font-size: 14px;
  }

  .title p {
    margin: 3px 0 0;
    color: var(--text-3);
    font-size: 12px;
  }

  .rollup {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .roll {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
  }

  .roll-icon {
    display: inline-flex;
  }

  .roll.model .roll-icon { color: var(--model); }
  .roll.tool .roll-icon { color: var(--warning); }
  .roll.approval .roll-icon { color: var(--queue); }

  .roll-text {
    display: grid;
    line-height: 1.2;
  }

  .roll-text strong {
    color: var(--text-1);
    font-size: 13px;
    font-variant-numeric: tabular-nums;
  }

  .roll-text small {
    color: var(--text-3);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .turns {
    min-height: 0;
    overflow-y: auto;
    padding: 12px 18px 18px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .turn-row {
    display: grid;
    grid-template-columns: minmax(190px, 240px) minmax(0, 1fr);
    gap: 14px;
    align-items: start;
    padding: 8px 0;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  }

  .turn-label {
    display: grid;
    grid-template-columns: auto auto;
    column-gap: 8px;
    align-items: baseline;
    padding-top: 4px;
  }

  .turn-no {
    color: var(--text-1);
    font-size: 12px;
    font-weight: 650;
  }

  .turn-dur {
    justify-self: end;
    color: var(--text-2);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }

  .turn-preview {
    grid-column: 1 / -1;
    margin-top: 2px;
    overflow: hidden;
    color: var(--text-3);
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .track {
    position: relative;
    min-height: 30px;
    border-radius: 6px;
    background: color-mix(in srgb, var(--surface-2) 55%, transparent);
  }

  .turn-body {
    min-width: 0;
    display: grid;
    gap: 8px;
  }

  .parent-track {
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--border) 45%, transparent);
  }

  .subagent-stack {
    display: grid;
    gap: 7px;
    padding-left: 12px;
    border-left: 1px solid color-mix(in srgb, var(--accent) 28%, transparent);
  }

  .subagent-row {
    display: grid;
    grid-template-columns: minmax(120px, 168px) minmax(0, 1fr);
    gap: 10px;
    align-items: start;
  }

  .subagent-label {
    min-width: 0;
    display: grid;
    gap: 1px;
    padding-top: 1px;
  }

  .subagent-label span {
    width: max-content;
    padding: 1px 5px;
    border: 1px solid color-mix(in srgb, var(--accent) 44%, transparent);
    border-radius: 4px;
    color: var(--accent);
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .subagent-label strong {
    min-width: 0;
    overflow: hidden;
    color: var(--text-2);
    font-size: 11px;
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .subagent-label small {
    color: var(--text-3);
    font-size: 10px;
    font-variant-numeric: tabular-nums;
  }

  .subagent-track {
    background: color-mix(in srgb, var(--surface-2) 36%, transparent);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 16%, transparent);
  }

  .bar {
    position: absolute;
    top: 3px;
    height: 20px;
    min-width: 6px;
    display: inline-flex;
    align-items: center;
    padding: 0 7px;
    border: 1px solid transparent;
    border-radius: 5px;
    color: var(--surface-0);
    cursor: pointer;
    font: inherit;
    font-size: 10px;
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    transition: filter 120ms ease, opacity 120ms ease, outline-color 120ms ease;
  }

  .bar-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .bar.model {
    border-color: color-mix(in srgb, var(--model) 75%, var(--surface-0));
    background: color-mix(in srgb, var(--model) 18%, var(--surface-0));
    color: var(--model);
  }

  .bar.tool { background: var(--warning); }
  .bar.approval { background: var(--queue); }
  .bar.done { background: var(--success); }
  .bar.error { background: var(--error); }

  .bar.ongoing {
    border-style: dashed;
  }

  .bar.future {
    opacity: 0.32;
  }

  .bar.active {
    outline: 2px solid var(--text-1);
    outline-offset: 1px;
  }

  .bar:hover {
    filter: brightness(1.12);
  }

  .track-empty,
  .empty {
    color: var(--text-3);
    font-size: 11px;
  }

  .track-empty {
    position: absolute;
    left: 8px;
    top: 6px;
  }

  .empty {
    padding: 20px 2px;
  }
</style>
