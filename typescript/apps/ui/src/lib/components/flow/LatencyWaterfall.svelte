<script lang="ts" module>
  import {
    playheadFraction,
    turnScale,
    type StepTimeline,
    type TurnTimeline
  } from "$lib/state/stepTimeline";
  import {
    distinctTickLabels,
    tickLabelBoxes,
    workflowScaleTicks,
    type ScaledSegment
  } from "$lib/state/workflowTimelineScale";
  import { formatDuration } from "$lib/state/replayLog";

  /**
   * One row's ruler: the Timeline's tick rule on a single unfolded segment, so a tick
   * is placed per `spacingPx` of track rather than five per row whatever its width, and
   * a label that would collide with the end label is dropped. `gap` is the guide pitch,
   * in percent of the track, taken from the nice step before the end tick is added.
   */
  export function axisTicks(
    scale: number,
    widthPx: number,
    labelWidth: (tick: number) => number
  ): { ticks: number[]; gap: number } {
    const segments: ScaledSegment[] = [
      { key: "turn", startTs: 0, endTs: scale, startPx: 0, endPx: widthPx, collapsed: false }
    ];
    const raw = workflowScaleTicks(segments);
    const ticks = tickLabelBoxes(segments, distinctTickLabels(raw, formatDuration), labelWidth).map(
      (box) => box.tick
    );
    const step = raw.length > 2 ? raw[1]! - raw[0]! : scale;
    return { ticks, gap: (step / scale) * 100 };
  }

  /**
   * Turns above which a row folds to a proportion bar instead of drawing its tracks.
   *
   * Row pitch is about 152px — track, ruler, gap, padding, border — so 200 turns is
   * 30,000px, or 89 screens of a 340px drawer. Twelve is where that starts to bite,
   * and high enough that today's two-turn sessions never meet the affordance.
   *
   * Module scope and exported, so
   * LatencyWaterfall.test.mjs asserts this number and the two rules below rather
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
  import { onDestroy, untrack } from "svelte";
  import type { Attachment } from "svelte/attachments";
  import { createVirtualizer, defaultRangeExtractor } from "@tanstack/svelte-virtual";
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";
  import Reading from "$lib/components/primitives/Reading.svelte";
  import type { Metric } from "$lib/components/primitives/metrics";
  import { ChevronDown, ChevronRight, Cpu, ShieldCheck, Wrench } from "@lucide/svelte";
  import { scrollFollower } from "$lib/state/followScroll";
  import type { ReplayMarker } from "$lib/state/replayLog";
  import { textWidth } from "$lib/state/textWidth";
  /* `turnScale` and `formatDuration` are not here: the module block above imports them
     for the collapse predicate and the ruler, and those bindings are in scope for this one. */
  import { HISTORY_GAP_NOTE } from "$lib/state/historyGap";
  import {
    aggregateSpans,
    denseLanes,
    indexAtFraction,
    spanNear,
    type LaneStrip,
    type SpanKind,
    type TimelineSpan
  } from "$lib/state/stepTimeline";

  interface Props {
    timeline: StepTimeline;
    viewIndex: number;
    anomalyMarkers?: ReplayMarker[];
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
    turn: TurnTimeline;
    subagent: boolean;
    /** Prepended to every bar's tooltip, naming the subagent a nested row belongs to. */
    prefix?: string;
    /** A subagent's own start and end, in seconds from `turnStart`, drawn as its band. */
    range?: { startTs: number; endTs: number };
    /**
     * This track's turn straddles a seam in the run's history, so "no spans" here is
     * not evidence that nothing ran. Parent tracks only — the seam is a root-log
     * discontinuity and has no meaning inside a child's own offsets.
     */
    gap?: boolean;
  }

  let { timeline, viewIndex, anomalyMarkers = [], onScrub }: Props = $props();

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

  /* The bar's horizontal padding and border. */
  const LABEL_PAD_PX = 16;
  /* The bar's own font, read from the tokens once rather than per bar. */
  let labelFont = "";

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
   * The count rides along because the instrument form drops the rollup, and a
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

  function font(): string {
    if (!labelFont && typeof document !== "undefined") {
      const root = getComputedStyle(document.documentElement);
      labelFont = `${root.getPropertyValue("--font-xs")} ${root.getPropertyValue("--font-sans")}`;
    }
    return labelFont;
  }

  /** Stolen from TanStack's `eventBarCanFitLabel`, in pixels rather than data units. */
  function fitsLabel(span: TimelineSpan, widthPct: number, trackPx: number): boolean {
    return (widthPct / 100) * trackPx >= textWidth(barText(span), font()) + LABEL_PAD_PX;
  }

  /* Digits as zeros: ticks render tabular figures, which a canvas measurement ignores. */
  const tickWidth = (tick: number) => textWidth(formatDuration(tick).replace(/\d/g, "0"), font());

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

  /* One word each: the header figures carry an icon, so the label only has to name it. */
  const kindWord: Record<SpanKind, string> = { model: "model", tool: "tool", approval: "approval", evaluation: "check" };

  const kindLabel: Record<SpanKind, string> = {
    model: "model",
    tool: "tool",
    approval: "approval",
    /* The automatic check nested inside an approval gate. Labelled apart from "approval"
       because the two answer different questions: how long the gate was open, versus how
       much of that was the approver rather than the person. */
    evaluation: "approval check"
  };

  const kindIcon: Record<SpanKind, Metric["icon"]> = { model: Cpu, tool: Wrench, approval: ShieldCheck, evaluation: ShieldCheck };
  const kindHue: Partial<Record<SpanKind, string>> = { model: "--model", tool: "--tool", approval: "--live" };

  const headLabel = $derived(
    `per step · ${timeline.turns.length.toLocaleString("en-US")} ${timeline.turns.length === 1 ? "turn" : "turns"}` +
      (subagentTurnCount > 0 ? ` · ${subagentTurnCount.toLocaleString("en-US")} nested` : "")
  );

  /* Led by the total, like Tokens, then one plain figure per kind. Count and share are
     in each figure's tip: on the strip they tripled every label, and the durations
     beside the total already say the share. A kind that rounds to 0% is drawn quiet. */
  const rollup: Metric[] = $derived([
    { label: "total", value: formatDuration(totalSpanSeconds), tone: "strong" },
    ...aggregates.map((agg): Metric => {
      const share = totalSpanSeconds > 0 ? Math.round((agg.totalSeconds / totalSpanSeconds) * 100) : 0;
      return {
        label: kindWord[agg.kind],
        value: formatDuration(agg.totalSeconds),
        tone: share === 0 ? "quiet" : "neutral",
        icon: kindIcon[agg.kind],
        hue: kindHue[agg.kind],
        note: `${kindLabel[agg.kind]} · ${agg.count.toLocaleString("en-US")}× · ${share}% of total`
      };
    })
  ]);

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

  /**
   * A track's bars and strips: lanes too crowded for a bar per span are binned into a strip,
   * and only the lanes with room draw a bar per span.
   *
   * Kept per spans array, because a turn's spans keep their identity until the fold
   * rebuilds that turn, and the snippet re-reads its track on every step of the cursor.
   */
  const trackCache = new WeakMap<
    TimelineSpan[],
    { key: string; strips: LaneStrip[]; bars: TimelineSpan[] }
  >();

  /* Before a track is measured, a guess at its width rather than zero, so a short turn
     does not flash as strips on its first frame. */
  const UNMEASURED_PX = 600;

  function trackLayout(track: TrackView): { strips: LaneStrip[]; bars: TimelineSpan[] } {
    const width = trackWidths[track.key] || UNMEASURED_PX;
    const key = `${width}:${track.scale}:${track.turnStart}`;
    const cached = trackCache.get(track.spans);
    if (cached?.key === key) return cached;
    const strips = denseLanes(track.spans, track.turnStart, track.scale, width);
    const dense = new Set(strips.map((strip) => strip.lane));
    const layout = { key, strips, bars: dense.size ? track.spans.filter((span) => !dense.has(span.lane)) : track.spans };
    trackCache.set(track.spans, layout);
    return layout;
  }

  /** The span under the pointer on a strip, drawn as a bar over it so its label shows. */
  let hovered = $state<{ key: string; span: TimelineSpan } | null>(null);

  /* How far either side of the pointer a strip looks for a span: the bar's hit slop. */
  const STRIP_SLOP_PX = 6;

  function spanUnder(event: MouseEvent, track: TrackView, strip: LaneStrip): TimelineSpan | null {
    const box = (event.currentTarget as Element).getBoundingClientRect();
    if (box.width === 0) return null;
    const perPx = track.scale / box.width;
    const at = track.turnStart + (event.clientX - box.left) * perPx;
    return spanNear(strip.spans, at - STRIP_SLOP_PX * perPx, at + STRIP_SLOP_PX * perPx);
  }

  /* A click lands on the span under the pointer; Enter or Space, which has no pointer, steps
     to the lane's next span after the cursor, so a strip is not a dead end for the keyboard. */
  function scrubStrip(event: MouseEvent, track: TrackView, strip: LaneStrip): void {
    const span =
      event.detail === 0
        ? (strip.spans.find((item) => item.startIndex > viewIndex) ?? strip.spans[0])
        : spanUnder(event, track, strip);
    if (span) onScrub(span.startIndex);
  }

  let scrubGesture: { element: HTMLElement; pointerId: number; turn: TurnTimeline } | null = null;
  let scrubFrame = 0;
  let pendingScrub: { element: HTMLElement; clientX: number; turn: TurnTimeline } | null = null;

  function scrubAt(element: HTMLElement, clientX: number, turn: TurnTimeline): void {
    const box = element.getBoundingClientRect();
    if (box.width === 0) return;
    let fraction = (clientX - box.left) / box.width;
    if (getComputedStyle(element).direction === "rtl") fraction = 1 - fraction;
    const index = indexAtFraction(turn, fraction);
    if (index != null) onScrub(index);
  }

  function startScrub(event: PointerEvent, turn: TurnTimeline): void {
    if (event.button !== 0 || !event.isPrimary) return;
    event.preventDefault();
    const element = event.currentTarget as HTMLElement;
    element.setPointerCapture(event.pointerId);
    scrubGesture = { element, pointerId: event.pointerId, turn };
    scrubAt(element, event.clientX, turn);
  }

  function moveScrub(event: PointerEvent): void {
    if (scrubGesture?.pointerId !== event.pointerId) return;
    pendingScrub = {
      element: scrubGesture.element,
      clientX: event.clientX,
      turn: scrubGesture.turn
    };
    if (scrubFrame) return;
    scrubFrame = requestAnimationFrame(() => {
      scrubFrame = 0;
      if (pendingScrub) scrubAt(pendingScrub.element, pendingScrub.clientX, pendingScrub.turn);
      pendingScrub = null;
    });
  }

  function stopScrub(event: PointerEvent): void {
    if (scrubGesture?.pointerId !== event.pointerId) return;
    if (scrubGesture.element.hasPointerCapture(event.pointerId)) {
      scrubGesture.element.releasePointerCapture(event.pointerId);
    }
    scrubGesture = null;
    pendingScrub = null;
    if (scrubFrame) cancelAnimationFrame(scrubFrame);
    scrubFrame = 0;
  }

  onDestroy(() => {
    if (scrubFrame) cancelAnimationFrame(scrubFrame);
  });

  /* Where along a strip its spans turn from past to future: the playhead, when the cursor is
     in this turn, else all or nothing. Bars fade to .32 past the cursor; a strip fades there. */
  function futureFrom(track: TrackView, strip: LaneStrip): number {
    return track.head ?? (viewIndex < strip.spans[0]!.startIndex ? 0 : 1);
  }

  /** Spans in a strip holding the cursor, drawn as bars so the active tint survives binning. */
  function activeIn(strips: LaneStrip[]): TimelineSpan[] {
    return strips.flatMap((strip) =>
      strip.spans.filter((span) => viewIndex >= span.startIndex && viewIndex <= span.endIndex)
    );
  }

  /* This pane's own scroller, and the only box the follow below may move. */
  let turnsElement = $state<HTMLElement | null>(null);
  const follower = scrollFollower(() => turnsElement);

  /* The row holding focus, kept in the DOM while it is scrolled out of view: unmounting it
     would drop focus to the body, and the next Tab would start from the top of the page. */
  let focusedIndex: number | null = null;

  /* Only the rows in view are in the DOM. Folding bounded a row's height, not its cost:
     12,500 folded turns were still 12,500 rows, each with its proportion bar. */
  const rows = createVirtualizer<HTMLElement, HTMLElement>({
    /* Seeded here as well as in the effect below, which SSR never runs. */
    count: untrack(() => timeline.turns.length),
    getItemKey: (index) => timeline.turns[index]?.turnNumber ?? index,
    estimateSize: (index) => {
      const turn = timeline.turns[index];
      return turn && timeline.turns.length <= COLLAPSE_THRESHOLD ? 60 + trackHeight(turn.laneCount) : 46;
    },
    getScrollElement: () => turnsElement,
    initialRect: { width: 0, height: 800 },
    gap: 8,
    overscan: 4,
    rangeExtractor: (range) => {
      const indexes = defaultRangeExtractor(range);
      if (focusedIndex == null || focusedIndex >= range.count || indexes.includes(focusedIndex)) return indexes;
      return [...indexes, focusedIndex].sort((a, b) => a - b);
    }
  });
  /* Untracked: setOptions publishes the store, which would re-run the effect calling it. */
  const rowList = () => untrack(() => $rows);
  const measureRow: Attachment<HTMLElement> = (node) => rowList().measureElement(node);

  /* Pre, so a shrunk list's count lands before the rows render and index past its end. */
  $effect.pre(() => {
    const count = timeline.turns.length;
    void turnsElement;
    rowList().setOptions({ count });
  });

  function trackFocus(event: FocusEvent): void {
    const row = (event.target as Element).closest<HTMLElement>("[data-index]");
    focusedIndex = row ? Number(row.dataset.index) : null;
  }

  function releaseFocus(event: FocusEvent): void {
    if (!(event.currentTarget as Element).contains(event.relatedTarget as Node | null)) focusedIndex = null;
  }

  /**
   * Keep the row holding the playhead on screen.
   *
   * The pane had no scroll-follow of any kind, so past a dozen turns `→` or `.` moved
   * a line in a row eighty screens away with no way to reach it. Still the same shape
   * as the Logs pane's follow, because it is now literally the same code: see
   * followScroll.ts for why `scrollIntoView` had to go from both of them. A row
   * windowed out of the DOM is scrolled to by index first, then followed.
   */
  $effect(() => {
    const index = timeline.turns.findIndex((turn) => playheadFraction(turn, viewIndex) != null);
    if (index < 0) return;
    follower.to(`waterfall-turn-${timeline.turns[index]!.turnNumber}`, () =>
      rowList().scrollToIndex(index, { align: "auto" })
    );
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

<!-- One span's bar. Over a strip it is a picture of the span rather than a control —
     the strip under it takes the pointer — so it says the same thing without a second
     target for the same click. -->
{#snippet bar(span: TimelineSpan, track: TrackView, overStrip = false)}
  {@const box = pct(span, track.turnStart, track.scale)}
  {@const fits = fitsLabel(span, box.width, trackWidths[track.key] ?? 0)}
  {#if overStrip}
    <span
      class={`${barClass(span, fits, box.left)} over-strip`}
      style={`left: ${box.left}%; width: ${box.width}%; top: ${laneTop(span.lane)}px`}
      aria-hidden="true"
    >
      <span class="bar-text"><bdi>{span.label}</bdi> · {formatDuration(span.durationSeconds)}</span>
    </span>
  {:else}
    <button
      class={barClass(span, fits, box.left)}
      style={`left: ${box.left}%; width: ${box.width}%; top: ${laneTop(span.lane)}px`}
      title={spanTitle(span, track.prefix)}
      onpointerdown={(event) => event.stopPropagation()}
      onclick={() => onScrub(span.startIndex)}
    >
      <span class="bar-text"><bdi>{span.label}</bdi> · {formatDuration(span.durationSeconds)}</span>
    </button>
  {/if}
{/snippet}

<!-- Both tracks in a turn — the parent's and each subagent's — are the same
     object: named lanes down the left, bars against the turn's scale. Written
     once and rendered twice. -->
{#snippet laneTrack(track: TrackView)}
  {@const layout = trackLayout(track)}
  {@const active = layout.strips.length > 0 ? activeIn(layout.strips) : []}
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
      role="presentation"
      style={`height: ${trackHeight(track.laneCount)}px`}
      bind:clientWidth={null, (value) => (trackWidths[track.key] = value ?? 0)}
      onpointerdown={(event) => startScrub(event, track.turn)}
      onpointermove={moveScrub}
      onpointerup={stopScrub}
      onpointercancel={stopScrub}
    >
      {#if track.range}
        {@const start = Math.max(0, ((track.range.startTs - track.turnStart) / track.scale) * 100)}
        <span
          class="subagent-band"
          style={`left: ${start}%; width: ${Math.max(0, Math.min(100 - start, ((track.range.endTs - track.range.startTs) / track.scale) * 100))}%`}
          aria-hidden="true"
        ></span>
      {/if}
      <!-- One button per dense lane, its spans traced as pixel runs. Its label says what a
           bar per span would have; the bars that matter right now — the one under the
           pointer and the ones holding the cursor — are drawn over it below. -->
      {#each layout.strips as strip (strip.lane)}
        <button
          class={`strip ${strip.kind}`}
          style={`top: ${laneTop(strip.lane)}px; --future: ${futureFrom(track, strip) * 100}%`}
          aria-label={`${kindLabel[strip.kind]} lane · ${strip.spans.length.toLocaleString()} spans` +
            `${strip.errors ? ` · ${strip.errors.toLocaleString()} failed` : ""}` +
            " · too dense to draw one by one; press to step to the next"}
          onpointerdown={(event) => event.stopPropagation()}
          onpointermove={(event) => {
            const span = spanUnder(event, track, strip);
            hovered = span ? { key: track.key, span } : null;
          }}
          onpointerleave={() => (hovered = null)}
          onclick={(event) => scrubStrip(event, track, strip)}
        >
          <svg viewBox={`0 0 ${trackWidths[track.key] || UNMEASURED_PX} 20`} preserveAspectRatio="none" aria-hidden="true">
            <path class="fill" d={strip.fill} />
            <path class="error" d={strip.error} />
            <path class="ongoing" d={strip.ongoing} />
          </svg>
        </button>
      {/each}

      {#each layout.bars as span (span.id)}
        {@render bar(span, track)}
      {/each}
      {#each active as span (span.id)}
        {@render bar(span, track, true)}
      {/each}
      {#if hovered?.key === track.key && !active.includes(hovered.span)}
        {@render bar(hovered.span, track, true)}
      {/if}

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
    <Reading label={headLabel} headline={rollup.slice(0, 1)} breakdown={rollup.slice(1)} />
  </header>

  <div class="turns" data-drawer-fit bind:this={turnsElement} onscroll={follower.handleScroll}>
    {#if timeline.turns.length === 0}
      <EmptyState title="Step through the stream to chart per-step latency." />
    {:else}
      <!-- No shared ruler heads these rows, because each is scaled to its own turn
           and no single ruler would be true of all of them. Nor does a sentence
           saying so go in the sticky slot: it would span the full width of a row,
           putting it across the track rather than beside it, and a caption lying
           over the bars it captions reads as a drawing error rather than a warning.
           The per-row rulers carry it instead — they are next to the bars, and a
           reader comparing 0s–40s against 0s–8m has the answer in the same
           glance. -->
      <!-- A feed of turns, windowed: each row says where it sits in the whole run, since
           most of the run is not in the DOM to count. -->
      <div
        class="turn-list"
        role="feed"
        aria-label="Turns"
        style:height={`${$rows.getTotalSize()}px`}
        onfocusin={trackFocus}
        onfocusout={releaseFocus}
      >
      {#each $rows.getVirtualItems() as item (item.key)}
        {@const turn = timeline.turns[item.index]}
        {#if turn}
        {@const scale = turnScale(turn)}
        {@const axis = axisTicks(scale, trackWidths[`turn-${turn.turnNumber}`] || UNMEASURED_PX, tickWidth)}
        {@const ticks = axis.ticks}
        {@const head = playheadFraction(turn, viewIndex)}
        {@const cues = anomalyMarkers.filter((marker) => marker.turnNumber === turn.turnNumber)}
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
          aria-label={`Turn ${turn.turnNumber}`}
          aria-posinset={item.index + 1}
          aria-setsize={timeline.turns.length}
          data-index={item.index}
          style={`--tick-gap: ${axis.gap}%; top: ${item.start}px`}
          {@attach measureRow}
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
              {#if head != null}
                <span
                  class="playhead"
                  style={`left: calc(18px + (100% - 18px) * ${head})`}
                  aria-hidden="true"
                ></span>
              {/if}

              <!-- One ruler per row, ending at that row's own duration: the axis is
                   where the difference between two rows' scales is actually read. -->
              <div class="track-wrap">
                <div class="lane-rail"></div>
                <div
                  class="axis"
                  role="presentation"
                  onpointerdown={(event) => startScrub(event, turn)}
                  onpointermove={moveScrub}
                  onpointerup={stopScrub}
                  onpointercancel={stopScrub}
                >
                  {#each ticks as tick, i (tick)}
                    <span
                      class="tick"
                      style={`left: ${(tick / scale) * 100}%`}
                      data-last={i === ticks.length - 1 ? "true" : undefined}
                    >
                      {formatDuration(tick)}
                    </span>
                  {/each}
                  {#each cues as marker (marker.id)}
                    {@const at = playheadFraction(turn, marker.index)}
                    {#if at != null}
                      <button
                        type="button"
                        class={`cue ${marker.tone}`}
                        style={`--at: ${at}`}
                        aria-label={`Jump to ${marker.label}, turn ${marker.turnNumber}`}
                        onpointerdown={(event) => event.stopPropagation()}
                        onclick={() => onScrub(marker.index)}
                      ></button>
                    {/if}
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
                turn,
                subagent: false,
                gap: turn.historyGap
              })}

              {#if turn.subagentTurns.length > 0}
                <div class="subagent-stack" aria-label={`Subagent latency for turn ${turn.turnNumber}`}>
                  {#each turn.subagentTurns as subagent (`${subagent.workflowId}:${subagent.turnNumber}`)}
                    <!-- Captioned above its track rather than beside it, at every width:
                         a label column here would start this track right of the parent's,
                         and the two share one time scale. -->
                    <div class="subagent-row">
                      <div class="subagent-label">
                        <span>Subagent</span>
                        <strong class="fade-end" dir="auto" title={subagent.label}>{subagent.label}</strong>
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
                        turn,
                        subagent: true,
                        prefix: subagent.label,
                        range: subagent
                      })}
                    </div>
                  {/each}
                </div>
              {/if}
            {/if}
          </div>
        </article>
        {/if}
      {/each}
      </div>
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
    container: waterfall / inline-size;
  }

  /* The divider is the scroller's edge, not a card: the turns scroll under it. */
  .waterfall-head {
    min-width: 0;
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
  }

  .turns {
    min-height: 0;
    overflow-y: auto;
    padding: 12px 18px 18px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .turn-list {
    position: relative;
    flex: 0 0 auto;
  }

  .turn-row {
    position: absolute;
    inset-inline: 0;
    display: grid;
    grid-template-columns: minmax(190px, 240px) minmax(0, 1fr);
    gap: 14px;
    align-items: start;
    padding: 8px 0;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  }

  /* Below the Timeline's breakpoint the label column would leave the track a sliver, so
     the label goes above the track and the track takes the row, whatever the host says. */
  @container waterfall (max-width: 559px) {
    .turn-row {
      grid-template-columns: minmax(0, 1fr);
      gap: 8px;
    }
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
  .lane-mark.tool { color: var(--tool); }
  .lane-mark.approval { color: var(--live); }

  .axis {
    position: relative;
    height: 22px;
    border-bottom: 1px solid var(--border);
  }

  .axis,
  .track {
    direction: ltr;
    cursor: grab;
    touch-action: none;
    user-select: none;
  }

  .axis:active,
  .track:active {
    cursor: grabbing;
  }

  .cue {
    position: absolute;
    inset-block-start: 0;
    inset-inline-start: calc(var(--at) * 100%);
    z-index: 3;
    width: 11px;
    height: 8px;
    margin-inline-start: -5.5px;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: pointer;
  }

  .cue::before {
    content: "";
    position: absolute;
    inset-block-start: 1px;
    inset-inline-start: 4px;
    width: 3px;
    height: 6px;
    background: var(--text-3);
  }

  .cue.approval::before {
    background: var(--live);
  }

  .cue.queue::before {
    background: var(--queue);
  }

  .cue.error::before {
    background: var(--error);
  }

  .cue:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -1px;
  }

  /* Where the transport's cursor falls in this row's wall-clock. Decorative: the
     scrubber under the rail is the control, and a second draggable time cursor on a
     different domain would be two answers to "where am I". It belongs to the row
     body so one line crosses the ruler and every lane. */
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
    outline: 2px solid var(--focus-ring);
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

  /* The kind tokens, stated again in this file after .lane-mark —
     a segment is not a `.bar`, which is absolutely positioned on a track and carries
     10px of hit slop, and inheriting all that to reuse three background declarations
     would cost more lines than repeating the triple the way the file already does. */
  .slice.model { background: var(--model); }
  .slice.tool { background: var(--tool); }
  .slice.approval { background: var(--live); }

  /* Clipped across only, so a bar's overflow label can never widen the scroller: at
     opacity 0 it is still layout, and one long tool name made the pane 12,000px wide. */
  .track {
    position: relative;
    overflow-x: clip;
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
    position: relative;
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
  }

  .subagent-row {
    min-width: 0;
    display: grid;
    gap: 3px;
  }

  /* Indented by the lane rail, so the caption starts where its track does. */
  .subagent-label {
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: 6px;
    padding-inline-start: 18px;
  }

  .subagent-label span,
  .subagent-label small {
    flex: none;
  }

  /* A subagent's own run inside the parent's turn, on the parent's scale: a fill and no
     stroke, as the Timeline's parent band. */
  .subagent-band {
    position: absolute;
    inset-block: 0;
    background: color-mix(in srgb, var(--surface-3) 35%, transparent);
    pointer-events: none;
  }

  .subagent-label span {
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
    color: var(--text-2);
    font-size: var(--font-sm);
    font-weight: 650;
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
    transition: filter var(--duration-fast) var(--ease-ui), opacity var(--duration-fast) var(--ease-ui), box-shadow var(--duration-fast) var(--ease-ui);
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
    max-width: min(40ch, 50cqi);
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

  .bar.tool { background: var(--tool); }
  .bar.approval { background: var(--live); }
  .bar.error { background: var(--error); }

  .bar.ongoing {
    border-style: dashed;
  }

  .bar.future {
    opacity: 0.32;
  }

  /* The playhead is what says where the cursor is now; this only has to say which
     span holds it, and in the app's own selection language — a tint, not a stroke.
     A --text-1 outline boxed every concurrent span in white, a stack of fifty of them
     at once, and outranked :focus-visible so a focused active bar lost its ring. A
     halo leaves `outline` to the focus ring, and the 2px fits the lane gap. */
  .bar.active {
    box-shadow: 0 0 0 2px var(--selected-bg);
  }

  /* Guarded rather than left decorative: the bar is a button that scrubs, and a
     brightness that sticks to the last one tapped competes with .bar.active,
     which is how the selected step is actually shown. */
  @media (hover: hover) and (pointer: fine) {
    .bar:hover {
      filter: brightness(1.12);
    }
  }

  /* A lane too dense for a bar per span: one control the width of the track, its spans
     painted as runs of pixels in the kind's color, failures and running spans over them. */
  .strip {
    position: absolute;
    left: 0;
    right: 0;
    height: 20px;
    padding: 0;
    border: 0;
    border-radius: var(--radius-sm);
    background: transparent;
    cursor: pointer;
  }

  .strip svg {
    display: block;
    width: 100%;
    height: 100%;
    /* `.bar.future`'s opacity, cut at the playhead rather than per bar. */
    mask-image: linear-gradient(to right, #000 var(--future), rgb(0 0 0 / 32%) var(--future));
  }

  .strip.tool .fill { fill: var(--tool); }
  .strip.model .fill { fill: color-mix(in srgb, var(--model) 45%, var(--surface-0)); }
  .strip.approval .fill { fill: var(--live); }
  .strip.evaluation .fill { fill: var(--queue); }
  .strip .error { fill: var(--error); }

  .strip .ongoing {
    fill: none;
    stroke: var(--text-1);
    stroke-dasharray: 2 2;
    vector-effect: non-scaling-stroke;
  }

  .strip:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 1px;
  }

  /* The span under the pointer, and any holding the cursor, drawn over the strip with
     their labels showing; the strip under them keeps the pointer. */
  .bar.over-strip {
    z-index: 3;
    pointer-events: none;
  }

  .bar.over-strip.unlabelled .bar-text {
    opacity: 1;
  }

  .track-empty {
    color: var(--text-3);
    font-size: var(--font-sm);
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
    border: 1px solid color-mix(in srgb, var(--error) 40%, var(--border));
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--error) 8%, transparent);
    color: var(--text-2);
    font-size: var(--font-xs);
    line-height: 1.45;
  }

  @media (prefers-reduced-motion: reduce) {
    /* Nothing here moves, but a bar's filter/opacity/tint fade is still a change
       the setting asks us not to animate. */
    .bar,
    .bar .bar-text {
      transition: none;
    }
  }
</style>
