<script lang="ts" module>
  import {
    playheadFraction,
    turnScale,
    type StepTimeline,
    type TurnTimeline
  } from "$lib/state/stepTimeline";

  /**
   * Turns above which a row folds to a proportion bar instead of drawing its tracks.
   *
   * Row pitch is about 152px — track, ruler, gap, padding, border — so 200 turns is
   * 30,000px, or 89 screens of a 340px drawer. Twelve is where that starts to bite,
   * and high enough that today's two-turn sessions never meet the affordance.
   *
   * Module scope and exported, the way TranscriptPanel exports statusKind(), so
   * check-waterfall-lanes.mjs asserts this number and the two rules below rather
   * than a copy that would pass forever while these rotted.
   */
  export const COLLAPSE_THRESHOLD = 12;

  /**
   * A turn the fold may not touch, whatever the threshold says: the cursor is inside
   * it, or something in it failed or is still in flight. A collapsed view that folds
   * away the turn that went wrong is worse than no collapse — the reader does not get
   * a worse answer, they get no sign the answer is there.
   *
   * Subagent spans count as their parent's, because that is the row they are drawn on.
   */
  export function turnHeldOpen(turn: TurnTimeline, viewIndex: number): boolean {
    if (playheadFraction(turn, viewIndex) != null) return true;
    /* A seam in the run's history is held open for the same reason a failure is: the
       fold's own argument is that a row with nothing to relate says as much folded,
       and a turn that cannot account for its own steps is precisely the row where
       that is untrue. Folded, its note would be the one thing the reader needed and
       the proportion bar would present the surviving fraction as the whole turn. */
    if (turn.historyGap) return true;
    return [...turn.spans, ...turn.subagentTurns.flatMap((sub) => sub.spans)].some(
      (span) => span.tone === "error" || span.ongoing
    );
  }

  /**
   * Whether a turn has a shape that a full row would show and a folded one would not.
   *
   * What the tall row buys is relationships: which lane a span is in, where it sits
   * along the turn's ruler, what it overlaps. A turn with one span has no relationship
   * to draw — the folded bar says "all of it, one kind" just as well — and a turn of
   * zero duration has no ruler to place anything on, so every bar in it stacks at the
   * left edge whatever the height. Those rows cost 152px and answer nothing, which is
   * what "past turns take up so much room" is pointing at: a 13s single-call turn and
   * two 0s turns were each as tall as an eight-minute one.
   *
   * Subagent spans count, because they are drawn on the parent's row.
   */
  export function turnWorthDrawing(turn: TurnTimeline): boolean {
    /* `turnScale` floors at a second so a row always has a ruler to draw, which means
       it cannot answer this question by itself: a turn that took no time at all still
       scales to 1. Above the floor the scale is real, and at the floor the turn's own
       duration is what says whether anything happened between its ends. */
    if (turnScale(turn) <= 1 && turn.durationSeconds <= 0) return false;
    const spans =
      turn.spans.length + turn.subagentTurns.reduce((total, sub) => total + sub.spans.length, 0);
    return spans > 1;
  }

  /**
   * Whether a turn draws its tracks. `opened` holds the turns the reader opened by
   * hand — the only state this feature keeps, and deliberately not in the URL: `?p=`
   * encodes which panes are where, and a stale link that re-folded a turn which had
   * auto-opened for an error is the exact failure the rule above exists to prevent.
   *
   * Two independent reasons to fold, and the held-open rules outrank both. The count
   * bounds a long session, where even rows worth reading have to give way to there
   * being two hundred of them. The shape rule bounds a short one, where the total is
   * fine and the problem is that a third of the rows are empty. Recency is not a rule
   * here on purpose: the newest turn of a live run is still running, so `ongoing`
   * already holds it open, and the turn the reader is actually reading is the one
   * under the playhead — both are covered above. Folding by age alone would have kept
   * an empty 0s turn open for being last while folding the rich one before it.
   */
  export function turnExpanded(
    turn: TurnTimeline,
    viewIndex: number,
    turnCount: number,
    opened: ReadonlySet<number>
  ): boolean {
    return (
      turnHeldOpen(turn, viewIndex) ||
      opened.has(turn.turnNumber) ||
      (turnCount <= COLLAPSE_THRESHOLD && turnWorthDrawing(turn))
    );
  }
</script>

<script lang="ts">
  import { ChevronDown, ChevronRight, Cpu, ShieldCheck, Wrench } from "@lucide/svelte";
  import { scrollFollower } from "$lib/state/followScroll";
  import { formatDuration } from "$lib/state/replayLog";
  import { niceTimeTicks } from "$lib/state/timeTicks";
  /* `turnScale` is not here: the module block above imports it for the collapse
     predicate, and that binding is in scope for this one. */
  import { HISTORY_GAP_NOTE } from "$lib/state/historyGap";
  import { aggregateSpans, type SpanKind, type TimelineSpan } from "$lib/state/stepTimeline";

  interface Props {
    timeline: StepTimeline;
    viewIndex: number;
    onScrub: (index: number) => void;
  }

  /** One track's worth of everything the shared lane snippet needs to draw it. */
  interface TrackView {
    /** Stable key for the measured width of this track. */
    key: string;
    spans: TimelineSpan[];
    laneCount: number;
    /** Seconds across the full width — the owning TURN's scale, not the track's. */
    scale: number;
    /** Time at x=0, always the parent turn's start, so nested rows line up with it. */
    turnStart: number;
    /** 0..1 along the scale, or null when the cursor is not in this turn. */
    head: number | null;
    subagent: boolean;
    /** Prepended to every bar's tooltip, naming the subagent a nested row belongs to. */
    prefix?: string;
    /**
     * This track's turn straddles a seam in the run's history, so "no spans" here is
     * not evidence that nothing ran. Parent tracks only — the seam is a root-log
     * discontinuity and has no meaning inside a child's own offsets.
     */
    gap?: boolean;
  }

  let { timeline, viewIndex, onScrub }: Props = $props();

  const aggregates = $derived(aggregateSpans(timeline));
  const totalSpanSeconds = $derived(
    aggregates.reduce((sum, agg) => sum + agg.totalSeconds, 0)
  );
  const subagentTurnCount = $derived(
    timeline.turns.reduce((sum, turn) => sum + turn.subagentTurns.length, 0)
  );

  /**
   * Each track's rendered width in px, measured rather than assumed.
   *
   * Whether a label fits is a question in pixels — "gpt-5.1 · 1m 12s" needs about
   * a hundred of them — and the bar only knows its width as a percentage of a
   * scale that now differs per row. One number per track answers it for every bar
   * in that track.
   */
  let trackWidths = $state<Record<string, number>>({});

  /* Roughly one character of --font-xs tabular text, plus the bar's own padding.
     ponytail: an estimate rather than a canvas measurement — being a few pixels
     pessimistic costs a label that would just have fitted, which lands on hover
     anyway; being optimistic is the truncation this replaced, so it rounds up. */
  const CHAR_PX = 6.6;
  const LABEL_PAD_PX = 16;

  function pct(
    span: TimelineSpan,
    turnStart: number,
    scale: number
  ): { left: number; width: number } {
    const left = Math.min(99.5, Math.max(0, ((span.startTs - turnStart) / scale) * 100));
    /* No floor. A width floor is a lie about duration told in the one place a
       reader goes to read duration; the bar's CSS min-width keeps it visible and
       its hit slop keeps it clickable, without widening the claim. */
    return { left, width: Math.max(0, Math.min(100 - left, (span.durationSeconds / scale) * 100)) };
  }

  function trackHeight(laneCount: number): number {
    return 6 + laneCount * 24;
  }

  function laneTop(lane: number): number {
    return 3 + lane * 24;
  }

  /**
   * The first lane each kind owns, which is where its rail glyph goes, and how many
   * spans of that kind this track holds.
   *
   * The count rides along because the instrument form drops the rollup cards, and a
   * glyph that can answer "how many tool calls in this turn" costs no pixels to ask.
   */
  function laneKinds(spans: TimelineSpan[]): { kind: SpanKind; lane: number; count: number }[] {
    const found = new Map<SpanKind, { lane: number; count: number }>();
    for (const span of spans) {
      const seen = found.get(span.kind);
      if (seen) {
        seen.lane = Math.min(seen.lane, span.lane);
        seen.count += 1;
      } else {
        found.set(span.kind, { lane: span.lane, count: 1 });
      }
    }
    return [...found]
      .map(([kind, seen]) => ({ kind, ...seen }))
      .sort((a, b) => a.lane - b.lane);
  }

  function spanState(span: TimelineSpan): "past" | "active" | "future" {
    if (viewIndex < span.startIndex) return "future";
    if (viewIndex >= span.startIndex && viewIndex <= span.endIndex) return "active";
    return "past";
  }

  function barText(span: TimelineSpan): string {
    return `${span.label} · ${formatDuration(span.durationSeconds)}`;
  }

  /** Stolen from TanStack's `eventBarCanFitLabel`, in pixels rather than data units. */
  function fitsLabel(span: TimelineSpan, widthPct: number, trackPx: number): boolean {
    return (widthPct / 100) * trackPx >= barText(span).length * CHAR_PX + LABEL_PAD_PX;
  }

  function barClass(span: TimelineSpan, fits: boolean, left: number): string {
    const tone = span.tone === "error" ? "error" : span.kind;
    return [
      "bar",
      tone,
      spanState(span),
      span.ongoing ? "ongoing" : "",
      fits ? "" : "unlabelled",
      /* An overflow label on a bar near the right edge would hang off the track. */
      !fits && left > 55 ? "flip" : ""
    ]
      .filter(Boolean)
      .join(" ");
  }

  function spanTitle(span: TimelineSpan, prefix?: string): string {
    const detail = span.detail ? ` · ${span.detail}` : "";
    return `${prefix ? `${prefix} · ` : ""}${barText(span)}${detail}`;
  }

  const kindLabel: Record<SpanKind, string> = {
    model: "model",
    tool: "tool",
    approval: "approval"
  };

  /** Turns the reader opened by hand. Ephemeral — see the note on `turnExpanded`. */
  let openedTurns = $state<ReadonlySet<number>>(new Set());

  function toggleTurn(turnNumber: number): void {
    const next = new Set(openedTurns);
    if (!next.delete(turnNumber)) next.add(turnNumber);
    openedTurns = next;
  }

  /**
   * One turn's time by kind, as a share of its own duration.
   *
   * `aggregateSpans` takes a timeline and one turn is a one-turn timeline, so the
   * folded row is the same arithmetic as the rollup rather than a second opinion. The
   * shares need not sum to 100%: the aggregate is exclusive, so time the turn spent
   * in none of the three leaves the remainder blank, truthfully.
   */
  function proportions(turn: TurnTimeline): { kind: SpanKind; width: number; title: string }[] {
    const scale = Math.max(turn.durationSeconds, 1);
    return aggregateSpans({ turns: [turn] }).map((agg) => ({
      kind: agg.kind,
      width: Math.min(100, (agg.totalSeconds / scale) * 100),
      title:
        `${kindLabel[agg.kind]} · ${formatDuration(agg.totalSeconds)} · ${agg.count}× · ` +
        `${Math.round((agg.totalSeconds / scale) * 100)}% of turn ${turn.turnNumber}`
    }));
  }

  /* This pane's own scroller, and the only box the follow below may move. */
  let turnsElement = $state<HTMLElement | null>(null);
  const follower = scrollFollower(() => turnsElement);

  /**
   * Keep the row holding the playhead on screen.
   *
   * The pane had no scroll-follow of any kind, so past a dozen turns `→` or `.` moved
   * a line in a row eighty screens away with no way to reach it. Still the same shape
   * as the Logs pane's follow, because it is now literally the same code: see
   * followScroll.ts for why `scrollIntoView` had to go from both of them.
   */
  $effect(() => {
    const followed = timeline.turns.find((turn) => playheadFraction(turn, viewIndex) != null);
    if (!followed) return;
    follower.to(`waterfall-turn-${followed.turnNumber}`);
  });
</script>

<!-- The turn number over its duration — the whole summary a folded row needs, and
     already what the label said. Written once because the label is a `<button>` when
     the row can fold and a `<div>` when it cannot. -->
{#snippet turnHeading(turn: TurnTimeline, canFold: boolean, expanded: boolean)}
  <span class="turn-no">
    Turn {turn.turnNumber}
    {#if canFold}
      <span class="turn-chevron" aria-hidden="true">
        {#if expanded}
          <ChevronDown size={13} />
        {:else}
          <ChevronRight size={13} />
        {/if}
      </span>
    {/if}
  </span>
  <span class="turn-dur">{formatDuration(turn.durationSeconds)}</span>
{/snippet}

<!-- Both tracks in a turn — the parent's and each subagent's — are the same
     object: named lanes down the left, bars against the turn's scale, one
     playhead. Written once and rendered twice. -->
{#snippet laneTrack(track: TrackView)}
  <div class="track-wrap">
    <div class="lane-rail" style={`height: ${trackHeight(track.laneCount)}px`}>
      {#each laneKinds(track.spans) as entry (entry.kind)}
        <span
          class={`lane-mark ${entry.kind}`}
          style={`top: ${laneTop(entry.lane)}px`}
          title={`${kindLabel[entry.kind]} lane · ${entry.count} ${entry.count === 1 ? "span" : "spans"}`}
        >
          {#if entry.kind === "model"}
            <Cpu size={11} />
          {:else if entry.kind === "tool"}
            <Wrench size={11} />
          {:else}
            <ShieldCheck size={11} />
          {/if}
        </span>
      {/each}
    </div>

    <div
      class="track"
      class:parent-track={!track.subagent}
      class:subagent-track={track.subagent}
      style={`height: ${trackHeight(track.laneCount)}px`}
      bind:clientWidth={null, (value) => (trackWidths[track.key] = value ?? 0)}
    >
      {#if track.head != null}
        <span class="playhead" style={`left: ${track.head * 100}%`} aria-hidden="true"></span>
      {/if}

      {#each track.spans as span (span.id)}
        {@const box = pct(span, track.turnStart, track.scale)}
        {@const fits = fitsLabel(span, box.width, trackWidths[track.key] ?? 0)}
        <button
          class={barClass(span, fits, box.left)}
          style={`left: ${box.left}%; width: ${box.width}%; top: ${laneTop(span.lane)}px`}
          title={spanTitle(span, track.prefix)}
          onclick={() => onScrub(span.startIndex)}
        >
          <span class="bar-text">{barText(span)}</span>
        </button>
      {/each}

      <!-- Suppressed over a seam, because there it is the bug: "no measured parent
           steps" reads as a turn that ran none, which is the one thing this track
           cannot tell apart from a turn whose steps were trimmed away. The note
           above the track says which this is. -->
      {#if track.spans.length === 0 && !track.gap}
        <span class="track-empty">
          no measured {track.subagent ? "subagent" : "parent"} steps
        </span>
      {/if}
    </div>
  </div>
{/snippet}

<section class="waterfall" aria-label="Latency waterfall">
  <header class="waterfall-head">
    <div class="title">
      <h2>Latency waterfall</h2>
      <p>
        Per-step wall-clock across {timeline.turns.length} parent turns
        {#if subagentTurnCount > 0}
          · {subagentTurnCount} nested subagent turns
        {/if}
        · each row on its own scale
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

  <div class="turns" bind:this={turnsElement} onscroll={follower.handleScroll}>
    {#if timeline.turns.length === 0}
      <p class="empty">Step through the stream to chart per-step latency.</p>
    {:else}
      <!-- No shared ruler heads these rows, because each is scaled to its own turn
           and no single ruler would be true of all of them. A sentence saying so
           used to sit in the sticky slot; it spanned the full width of a row, which
           put it across the track rather than beside it, and a caption lying over
           the bars it captions reads as a drawing error rather than a warning. The
           per-row rulers carry it now — they are next to the bars, and a reader
           comparing 0s–40s against 0s–8m has the answer in the same glance. -->
      {#each timeline.turns as turn (turn.turnNumber)}
        {@const scale = turnScale(turn)}
        {@const ticks = niceTimeTicks(scale, 5)}
        {@const head = playheadFraction(turn, viewIndex)}
        {@const expanded = turnExpanded(turn, viewIndex, timeline.turns.length, openedTurns)}
        <!-- A turn held open by the rules above offers no toggle, because a control
             promising to fold away the failure would be lying. Otherwise the toggle
             appears exactly where the default would fold the row — which is both how
             a folded turn is opened by hand and how one opened by hand is put back. A
             row the default leaves open has nothing to offer and stays a plain label. -->
        {@const canFold =
          !turnHeldOpen(turn, viewIndex) &&
          !(timeline.turns.length <= COLLAPSE_THRESHOLD && turnWorthDrawing(turn))}
        <article
          id={`waterfall-turn-${turn.turnNumber}`}
          class="turn-row"
          class:collapsed={!expanded}
          style={`--tick-gap: ${100 / Math.max(ticks.length - 1, 1)}%`}
        >
          {#if canFold}
            <button
              class="turn-label turn-toggle"
              type="button"
              aria-expanded={expanded}
              aria-controls={`waterfall-turn-${turn.turnNumber}-body`}
              onclick={() => toggleTurn(turn.turnNumber)}
            >
              {@render turnHeading(turn, true, expanded)}
            </button>
          {:else}
            <div class="turn-label">{@render turnHeading(turn, false, expanded)}</div>
          {/if}
          <div class="turn-body" id={`waterfall-turn-${turn.turnNumber}-body`}>
            {#if !expanded}
              <!-- The whole row body, folded: where this turn's time went, on the
                   turn's own duration. The label already carries the two things worth
                   reading at this size, so nothing here repeats them. -->
              <div class="proportion" aria-hidden="true">
                {#each proportions(turn) as slice (slice.kind)}
                  <span class={`slice ${slice.kind}`} style={`width: ${slice.width}%`} title={slice.title}></span>
                {/each}
              </div>
            {:else}
              <!-- One ruler per row, ending at that row's own duration: the axis is
                   where the difference between two rows' scales is actually read. -->
              <div class="track-wrap">
                <div class="lane-rail"></div>
                <div class="axis" role="presentation">
                  {#each ticks as tick, i (tick)}
                    <span
                      class="tick"
                      style={`left: ${(tick / scale) * 100}%`}
                      data-last={i === ticks.length - 1 ? "true" : undefined}
                    >
                      {formatDuration(tick)}
                    </span>
                  {/each}
                </div>
              </div>

              <!-- One note at the seam, on the turn that straddles it, rather than a
                   badge on every turn the missing events might have touched: the
                   offsets say where the history breaks and nothing about what was in
                   the hole, so anything wider would be claiming more than is known. -->
              {#if turn.historyGap}
                <p class="history-gap">{HISTORY_GAP_NOTE}</p>
              {/if}

              {@render laneTrack({
                key: `turn-${turn.turnNumber}`,
                spans: turn.spans,
                laneCount: turn.laneCount,
                scale,
                turnStart: turn.startTs,
                head,
                subagent: false,
                gap: turn.historyGap
              })}

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
                      {@render laneTrack({
                        key: `sub-${subagent.workflowId}-${subagent.turnNumber}`,
                        spans: subagent.spans,
                        laneCount: subagent.laneCount,
                        scale,
                        turnStart: turn.startTs,
                        head,
                        subagent: true,
                        prefix: subagent.label
                      })}
                    </div>
                  {/each}
                </div>
              {/if}
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

  /* The glyph rail and the thing it labels, so a ruler and the track under it start
     at the same x and a tick lands where the bar it measures does. */
  .track-wrap {
    display: grid;
    grid-template-columns: 16px minmax(0, 1fr);
    gap: 2px;
    align-items: start;
  }

  .lane-rail {
    position: relative;
  }

  /* Named lanes: which row is model, which is tool, which is approval, said once
     down the side rather than inferred from the colour of whatever happens to be
     in it. A kind with nothing in this track has no lane and no glyph. */
  .lane-mark {
    position: absolute;
    left: 0;
    display: inline-flex;
    align-items: center;
    height: 20px;
    opacity: 0.75;
  }

  .lane-mark.model { color: var(--model); }
  .lane-mark.tool { color: var(--warning); }
  .lane-mark.approval { color: var(--queue); }

  .axis {
    position: relative;
    height: 16px;
    border-bottom: 1px solid var(--border);
  }

  /* Where the transport's cursor falls in this row's wall-clock. Decorative: the
     scrubber under the rail is the control, and a second draggable time cursor on a
     different domain would be two answers to "where am I". */
  .playhead {
    position: absolute;
    top: 0;
    bottom: 0;
    z-index: 2;
    width: 1px;
    background: var(--text-1);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--surface-0) 60%, transparent);
    pointer-events: none;
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

  /* The label is the disclosure control: it is already the biggest target in the row
     and the one thing a folded row is read by, so a separate chevron button beside it
     would be a second, smaller target for the same job. The rules below only undo the
     UA button, as .line-toggle does in TranscriptPanel; the grid still comes from
     .turn-label and from the host, which narrows this column to 68px in the drawer. */
  .turn-toggle {
    border: 0;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .turn-toggle:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--accent) 55%, transparent);
    outline-offset: 2px;
    border-radius: var(--radius-xs);
  }

  /* Inside .turn-no rather than a third grid child: the drawer narrows this label to
     one 68px column, where a third child would take a row of its own and hand the
     folded row back the height the fold just saved. */
  .turn-chevron {
    display: inline-flex;
    color: var(--text-3);
    vertical-align: -2px;
  }

  .turn-toggle:focus-visible .turn-chevron {
    color: var(--text-1);
  }

  @media (hover: hover) and (pointer: fine) {
    .turn-toggle:hover .turn-chevron {
      color: var(--text-1);
    }
  }

  /* Spans, not the paragraphs these were: the label is a `<button>` when the row can
     fold, and a `<p>` inside one is not allowed markup. That also retires the
     `margin: 0` these carried to stop two stacked UA margins putting 24px between a
     turn and its own duration in the drawer's narrow column. */
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

  /* The folded row's entire body: one 20px bar, three segments, on the turn's own
     duration. Row pitch goes from about 152px to about 46px. */
  .proportion {
    height: 20px;
    display: flex;
    overflow: hidden;
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--surface-2) 55%, transparent);
  }

  /* The kind tokens, stated the third time in this file after .lane-mark and .roll —
     a segment is not a `.bar`, which is absolutely positioned on a track and carries
     10px of hit slop, and inheriting all that to reuse three background declarations
     would cost more lines than repeating the triple the way the file already does. */
  .slice.model { background: var(--model); }
  .slice.tool { background: var(--warning); }
  .slice.approval { background: var(--queue); }

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

  /* Painted at its true width, down to the 2px that keeps a sub-second span on
     screen at all. The old 1.5%-of-the-run floor made every short span claim a
     duration it did not have, in the one pane whose whole job is duration. */
  .bar {
    position: absolute;
    top: 3px;
    height: 20px;
    min-width: 2px;
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
    /* Visible, so the hit slop below and an overflow label can leave the bar. The
       text is still held inside it by the flex box, not by clipping. */
    overflow: visible;
    transition: filter var(--duration-fast) var(--ease-ui), opacity var(--duration-fast) var(--ease-ui), outline-color var(--duration-fast) var(--ease-ui);
  }

  /* Hit slop, TanStack example 91's 44px handle target adapted: the TARGET grows,
     the bar does not. A 2px bar is a 22px-wide, 24px-tall thing to point at, which
     is the difference between a span you can read about and one you cannot catch.
     Kept inside the lane pitch so slop never steals the lane above or below. */
  .bar::before {
    content: "";
    position: absolute;
    inset: -2px -10px;
  }

  .bar-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Too narrow for its string. Rather than clip it to `gpt-5.1 · 1...`, the label
     comes out of the bar and sits beside it on hover or focus — the whole name and
     the whole duration, or nothing. */
  .bar.unlabelled {
    padding: 0;
  }

  .bar.unlabelled .bar-text {
    position: absolute;
    top: 50%;
    left: calc(100% + 6px);
    z-index: 9;
    padding: 1px 6px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-xs);
    background: var(--surface-3);
    color: var(--text-1);
    opacity: 0;
    pointer-events: none;
    transform: translateY(-50%);
    transition: opacity var(--duration-fast) var(--ease-ui);
  }

  /* Near the right-hand end there is no room to the right. */
  .bar.unlabelled.flip .bar-text {
    left: auto;
    right: calc(100% + 6px);
  }

  .bar.unlabelled:hover,
  .bar.unlabelled:focus-visible {
    z-index: 8;
  }

  .bar.unlabelled:hover .bar-text,
  .bar.unlabelled:focus-visible .bar-text {
    opacity: 1;
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

  /* Was a 2px outline offset by one, which on a hairline bar drew a white box
     several times the size of the thing it was pointing at. The playhead is what
     says where the cursor is now; this only has to say which span holds it. */
  .bar.active {
    outline: 1px solid var(--text-1);
    outline-offset: 0;
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

  /* Toned like an error because a hole in the history is a degradation, matching the
     subagent_stream_unavailable marker that says the same kind of thing; not shouted,
     because nothing here is broken now and there is nothing to act on. */
  .history-gap {
    margin: 0 0 6px;
    padding: 5px 8px;
    border-left: 2px solid var(--error);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--error) 8%, transparent);
    color: var(--text-2);
    font-size: var(--font-xs);
    line-height: 1.45;
  }

  .empty {
    padding: 20px 2px;
  }

  @media (prefers-reduced-motion: reduce) {
    /* Nothing here moves, but a bar's filter/opacity fade is still a change
       the setting asks us not to animate. */
    .bar,
    .bar .bar-text {
      transition: none;
    }
  }
</style>
