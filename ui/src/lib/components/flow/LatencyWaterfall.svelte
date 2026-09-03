<script lang="ts">
  import { Cpu, ShieldCheck, Wrench } from "@lucide/svelte";
  import { formatDuration } from "$lib/state/replayLog";
  import { niceTimeTicks } from "$lib/state/timeTicks";
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

  /* The shared horizontal scale every track is drawn against. */
  const scale = $derived(Math.max(timeline.maxTurnDuration, 1));

  /* The panel's header has always claimed the bars share one time scale; these are the
     ticks that let a duration actually be read off it. Evenly spaced by construction,
     which is what lets the track guides be a single repeating gradient. */
  const ticks = $derived(niceTimeTicks(scale, 6));

  /* A bar narrower than this cannot hold even a couple of characters, and CSS clipping
     turns its label into a one-character stub. Bare is more legible; the title still
     answers for it. */
  const MIN_LABEL_WIDTH_PCT = 7;

  function pct(span: TimelineSpan, turnStart: number): { left: number; width: number } {
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

  function barClass(span: TimelineSpan, width: number): string {
    const tone = span.tone === "error" ? "error" : span.kind;
    return [
      "bar",
      tone,
      spanState(span),
      span.ongoing ? "ongoing" : "",
      width < MIN_LABEL_WIDTH_PCT ? "unlabelled" : ""
    ]
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
      {#each aggregates as agg (agg.kind)}
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
            <small class="kicker">
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

  <div class="turns" style={`--tick-gap: ${100 / Math.max(ticks.length - 1, 1)}%`}>
    {#if timeline.turns.length === 0}
      <p class="empty">Step through the stream to chart per-step latency.</p>
    {:else}
      <div class="turn-row axis-row">
        <div class="axis-spacer"></div>
        <div class="axis" role="presentation">
          {#each ticks as tick, i (tick)}
            <span class="tick" style={`left: ${(tick / scale) * 100}%`} data-last={i === ticks.length - 1 ? "true" : undefined}>
              {formatDuration(tick)}
            </span>
          {/each}
        </div>
      </div>

      {#each timeline.turns as turn (turn.turnNumber)}
        <article class="turn-row">
          <div class="turn-label">
            <p class="turn-no">Turn {turn.turnNumber}</p>
            <p class="turn-dur">{formatDuration(turn.durationSeconds)}</p>
          </div>
          <div class="turn-body">
            <div class="track parent-track" style={`height: ${trackHeight(turn.laneCount)}px`}>
              {#each turn.spans as span (span.id)}
                {@const box = pct(span, turn.startTs)}
                <button
                  class={barClass(span, box.width)}
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
                {#each turn.subagentTurns as subagent (`${subagent.workflowId}:${subagent.turnNumber}`)}
                  <div class="subagent-row">
                    <div class="subagent-label">
                      <span>Subagent</span>
                      <strong title={subagent.label}>{subagent.label}</strong>
                      <small>
                        turn {subagent.turnNumber} · {formatDuration(subagent.durationSeconds)}
                      </small>
                    </div>
                    <div class="track subagent-track" style={`height: ${trackHeight(subagent.laneCount)}px`}>
                      {#each subagent.spans as span (span.id)}
                        {@const box = pct(span, turn.startTs)}
                        <button
                          class={barClass(span, box.width)}
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
    font-size: var(--font-xl);
  }

  .title p {
    margin: 3px 0 0;
    color: var(--text-3);
    font-size: var(--font-md);
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
    border-radius: var(--radius-md);
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
    font-size: var(--font-lg);
    font-variant-numeric: tabular-nums;
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

  /* The ruler carries .turn-row so it inherits whatever column layout the host has
     imposed on the rows it measures — the right pane collapses them to one column, and
     an axis that did not follow would point at the wrong place. The spacer occupies the
     label column when there are two, and collapses to nothing when there is one. */
  .turn-row.axis-row {
    position: sticky;
    top: 0;
    z-index: 1;
    align-items: end;
    padding: 0 0 4px;
    border-bottom: none;
    background: var(--surface-0);
  }

  .axis-spacer {
    height: 0;
  }

  .axis {
    position: relative;
    height: 16px;
    border-bottom: 1px solid var(--border);
  }

  .tick {
    position: absolute;
    bottom: 2px;
    color: var(--text-3);
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
    transform: translateX(-50%);
    white-space: nowrap;
  }

  /* The end labels would otherwise hang outside the track they belong to. */
  .tick:first-child {
    transform: none;
  }

  .tick[data-last="true"] {
    transform: translateX(-100%);
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
    font-size: var(--font-md);
    font-weight: 650;
  }

  .turn-dur {
    justify-self: end;
    color: var(--text-2);
    font-size: var(--font-md);
    font-variant-numeric: tabular-nums;
  }

  .turn-preview {
    grid-column: 1 / -1;
    margin-top: 2px;
    overflow: hidden;
    color: var(--text-3);
    font-size: var(--font-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .track {
    position: relative;
    min-height: 30px;
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--surface-2) 55%, transparent);
    /* Guides under the bars, one gradient rather than a node per line. The ticks are
       evenly spaced, so a repeat is exact. */
    background-image: repeating-linear-gradient(
      to right,
      color-mix(in srgb, var(--border) 55%, transparent) 0 1px,
      transparent 1px var(--tick-gap)
    );
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
    border-radius: var(--radius-xs);
    color: var(--accent);
    font-size: var(--font-2xs);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .subagent-label strong {
    min-width: 0;
    overflow: hidden;
    color: var(--text-2);
    font-size: var(--font-sm);
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .subagent-label small {
    color: var(--text-3);
    font-size: var(--font-xs);
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
    border-radius: var(--radius-sm);
    color: var(--surface-0);
    cursor: pointer;
    font: inherit;
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    transition: filter var(--duration-fast) var(--ease-ui), opacity var(--duration-fast) var(--ease-ui), outline-color var(--duration-fast) var(--ease-ui);
  }

  .bar-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Too narrow to read: drop the text rather than clip it to a stub. */
  .bar.unlabelled {
    padding: 0;
  }

  .bar.unlabelled .bar-text {
    display: none;
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

  /* Guarded rather than left decorative: the bar is a button that scrubs, and a
     brightness that sticks to the last one tapped competes with .bar.active,
     which is how the selected step is actually shown. */
  @media (hover: hover) and (pointer: fine) {
    .bar:hover {
      filter: brightness(1.12);
    }
  }

  .track-empty,
  .empty {
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .track-empty {
    position: absolute;
    left: 8px;
    top: 6px;
  }

  .empty {
    padding: 20px 2px;
  }

  @media (prefers-reduced-motion: reduce) {
    /* Nothing here moves, but a bar's filter/opacity fade is still a change
       the setting asks us not to animate. */
    .bar {
      transition: none;
    }
  }
</style>
