<script lang="ts">
  import { PanelBottom, Pause, Play, SkipBack, SkipForward } from "@lucide/svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import type { PlaybackSpeed } from "$lib/state/agentRun.svelte";
  import { dismissable } from "$lib/state/dismissable.svelte";
  import { eventVelocity, velocityPath } from "$lib/state/eventVelocity";
  import type { ReplayLogRow, ReplayMarker } from "$lib/state/replayLog";

  interface Props {
    viewIndex: number;
    total: number;
    playing: boolean;
    following: boolean;
    playbackSpeed: PlaybackSpeed;
    currentEvent: ReplayLogRow | null;
    turnMarkers: Array<{ index: number; turnNumber: number }>;
    anomalyMarkers: ReplayMarker[];
    /**
     * Every event of the run, for the density ribbon behind the bar. Only its
     * indices and timestamps are read; the rows are taken as they are so the
     * lane costs no second pass over the log.
     */
    eventRows: ReplayLogRow[];
    /**
     * Whether the bottom drawer is showing anything. Its control lives here
     * because the transport is the drawer's top edge — the drawer collapses
     * down onto this bar, so this bar is the lid, and the handle belongs on the
     * lid rather than inside the box.
     */
    drawerOpen: boolean;
    onToggleDrawer: () => void;
    onPlay: () => void;
    onPause: () => void;
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
    turnMarkers,
    anomalyMarkers,
    eventRows,
    drawerOpen,
    onToggleDrawer,
    onPlay,
    onPause,
    onSpeedChange,
    onJumpToLive,
    onReset,
    onScrub
  }: Props = $props();

  const playbackSpeeds: PlaybackSpeed[] = [1, 2, 5, 10];

  let detailOpen = $state(false);
  /* The lane already carries one floating label. A cue gets its name by taking
     that label over while the pointer is on it, rather than opening a second
     bubble in the same 18px of vertical space.
     Held as an event index, not as the marker object: the markers are re-derived
     from the log on every arriving event, so object identity does not survive
     even one frame of streaming. */
  let aimedIndex = $state<number | null>(null);

  const tone = $derived(currentEvent?.tone ?? "neutral");
  const eventTitle = $derived(currentEvent?.label ?? "Replay start");
  const eventType = $derived(currentEvent?.event ?? null);
  const eventBody = $derived(currentEvent?.body ?? currentEvent?.status ?? "");
  const hasDetail = $derived(Boolean(eventBody || eventType));

  const nextSpeed = $derived(
    playbackSpeeds[(playbackSpeeds.indexOf(playbackSpeed) + 1) % playbackSpeeds.length]
  );

  /**
   * The scale the lane is drawn against, held still while the pointer is on it.
   *
   * Every mark on the bar sits at ``index / total``, so each event that arrives
   * moves all of them: measured at streaming speed a cue slides left about 50px
   * a second. The mark you are reaching for leaves before you get there, which
   * is why a cue could be neither hovered nor clicked during a live run — the
   * pointer was always over whatever had moved into its place. Pinning the
   * denominator while the pointer is on the lane makes the bar a still target;
   * it catches up when the pointer leaves. The cost is that the bar is a
   * snapshot while you point at it, which is also what makes it aimable.
   */
  let heldScale = $state<number | null>(null);
  const scale = $derived(Math.max(heldScale ?? total, 0));
  /* A run with no events has no track to divide by, and dividing by the 1 the
     scale used to be floored at is what put "0/1" beside an aria text reading
     "0 of 0": two readings of one empty run, disagreeing about how long it is. */
  const cursorPct = $derived(scale === 0 ? 0 : Math.min((viewIndex / scale) * 100, 100));
  /* The reading agrees with the playhead, which already stops at the end of the
     lane: while the scale is held the run can pass it, and a position past the
     end would both disagree with where the playhead is drawn and need a digit
     the row has not reserved. Off the lane this is just viewIndex. */
  const shownIndex = $derived(Math.min(viewIndex, scale));
  const currentTurn = $derived(currentEvent?.turnNumber ?? null);

  /* Marks past a held scale have nowhere to sit — the lane does not clip, so
     they would ride out over the readout. They return with the scale. */
  const cues = $derived(anomalyMarkers.filter((marker) => marker.index <= scale));

  /* Where the run sped up and where it stalled, against the same scale the cues
     are placed on. Independent of the cursor, so scrubbing and playback never
     recompute it — only an arriving event or a released scale can. */
  const ridge = $derived(velocityPath(eventVelocity(eventRows, scale)));

  const aimedCue = $derived(
    aimedIndex == null ? null : (cues.find((cue) => cue.index === aimedIndex) ?? null)
  );
  const labelTone = $derived(aimedCue?.tone ?? tone);
  const labelTitle = $derived(aimedCue?.label ?? eventTitle);
  const labelTurn = $derived(aimedCue ? aimedCue.turnNumber : currentTurn);
  const labelPct = $derived(
    aimedCue && scale > 0 ? Math.min((aimedCue.index / scale) * 100, 100) : cursorPct
  );

  function holdScale(event: PointerEvent): void {
    /* Touch has no hover to end, so a tap would pin the scale until the next. */
    if (event.pointerType === "touch") return;
    heldScale = Math.max(total, 0);
  }

  function releaseScale(): void {
    heldScale = null;
  }

  /* A range input announces "96" on its own, which tells a screen-reader user
     nothing about where they are in the run. Say the event instead.
     "Event" throughout this row, and it means one published event of the run —
     the same thing the total counts. */
  const positionText = $derived(
    [
      currentTurn != null ? `Turn ${currentTurn}` : null,
      eventTitle,
      `event ${viewIndex} of ${total}`,
      following ? "live" : null
    ]
      .filter(Boolean)
      .join(", ")
  );

  /* Turns are the chapters of the track: each carries its own fill so the bar
     reads as a sequence rather than one undifferentiated line. A run with no
     events has no track to place them on, so there is nothing to divide by. */
  const turnSegments = $derived(
    scale === 0
      ? []
      : turnMarkers.map((marker, position) => {
          const startIndex = marker.index;
          const endIndex =
            position + 1 < turnMarkers.length ? turnMarkers[position + 1].index : scale;
          const length = Math.max(endIndex - startIndex, 0);
          const filled = length === 0 ? 0 : (viewIndex - startIndex) / length;
          return {
            turnNumber: marker.turnNumber,
            leftPct: (startIndex / scale) * 100,
            widthPct: (length / scale) * 100,
            fillPct: Math.min(Math.max(filled, 0), 1) * 100
          };
        })
  );

  function handleInput(event: Event): void {
    const index = Number((event.currentTarget as HTMLInputElement).value);
    /* A focused scrubber still moves itself for the keys the binding table does
       not spell — up, down, PageUp, PageDown — which are left native for screen
       reader users by omission rather than by a list. `onScrub` does the moving,
       so the run-state API learns nothing about keyboards.

       The keys the table does spell no longer arrive here at all: deference used
       to be decided by key name, which handed `Shift+←` to the slider as if it
       were a bare arrow and stepped one event where the reader asked for one
       turn. Those keys now win and cancel the native step. */
    onScrub(index);
  }

  /* Escape and press-outside come from the shared attachment, which is attached to the
     card itself: the card is rendered only while it is open, so mounting is opening and
     there is no second copy of `detailOpen` to keep in sync. `keep` is the whole footer
     because the reader goes on scrubbing while the card reports the scrub — a press on
     the transport is not a press somewhere else. */
</script>

<footer class="step-controller">
  <!-- Three controls, and what is missing is the point. Relative movement — one
       event or one turn, either direction — is keyboard only: the arrows and
       Shift+arrows do it, the `?` overlay is where they are written down, and
       four buttons that only repeated them cost 136px of a row the lane has to
       share. What survives is the pair of absolute destinations, which
       have no key-repeat to be worn out by, and the one toggle.

       Said the other way: the two that stayed carry state a button is the only
       thing that can show — play/pause is which of the two it is, follow dims
       once the view is already at the live edge. A stepper carries none, which
       is exactly why it survives being a key and nothing else.

       The rule below keeps the speed cycler out of that run: it is the only
       control here that changes how playback behaves rather than where the
       cursor is. -->
  <div class="transport">
    <IconButton label="Jump to first step" onclick={onReset} disabled={viewIndex === 0}>
      <SkipBack size={14} />
    </IconButton>
    {#if playing}
      <IconButton label="Pause replay" tone="primary" onclick={onPause}>
        <Pause size={16} />
      </IconButton>
    {:else}
      <IconButton label="Play replay" tone="primary" onclick={onPlay}>
        <Play size={16} />
      </IconButton>
    {/if}
    <!-- Not a toggle, so no `pressed`: this only ever seeks to the end, and
         pressing it while it was showing pressed left following true — there
         was no second state to reach. What it announced as a toggle state was
         really "the cursor is at the end", which is what the inert state says.
         The tailing that follows from being there is the part nobody could see,
         so the tip is where it gets said.

         `aria-disabled` rather than `disabled`, and the tip is the whole
         reason: the dimming exists to explain that the view is already at the
         live edge and will stay there, and `disabled` would drop the button out
         of the tab order — deleting that explanation for a keyboard user at
         precisely the moment it becomes true. So the button stays reachable and
         announces itself as unavailable, which is what it is. Nothing guards
         the handler because there is nothing to guard: jumpToLive() from the
         live edge is goTo(total) from total, which moves no cursor and changes
         no flag (pinned in check-turn-navigation.mjs). -->
    <IconButton
      label="Jump to latest step"
      tip="Jump to latest step — new events keep the view here"
      tone="follow"
      aria-disabled={following}
      onclick={onJumpToLive}
    >
      <SkipForward size={14} />
    </IconButton>
    <span class="rule" aria-hidden="true"></span>
    <!-- Four speeds cycle from one chip, the way podcast players do it. -->
    <Chip
      class="speed"
      tone={playbackSpeed === 1 ? "neutral" : "accent"}
      active={playbackSpeed !== 1}
      aria-label={`Playback speed ${playbackSpeed}x, switch to ${nextSpeed}x`}
      data-tip={`Playback speed ${playbackSpeed}× — click for ${nextSpeed}×`}
      onclick={() => onSpeedChange(nextSpeed)}
    >
      {playbackSpeed}×
    </Chip>
  </div>

  <!-- The lane is not a control — the range input inside it is. These handlers
       only decide what the lane is drawn against while a hand is over it, so
       there is no role here for anything to operate. -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="scrub" onpointerenter={holdScale} onpointerleave={releaseScale}>
    <!-- The label rides the playhead on hover — or the cue being pointed at, so
         a mark can say what it is without a second bubble over the same lane. -->
    <div
      class="tip"
      style={`left: ${labelPct}%; transform: translateX(-${labelPct}%)`}
      aria-hidden="true"
    >
      <span class={`dot ${labelTone}`}></span>
      <span class="tip-title">{labelTitle}</span>
      {#if labelTurn != null}
        <span class="kicker tip-turn">turn {labelTurn}</span>
      {/if}
      {#if aimedCue}
        <span class="kicker tip-hint">jump</span>
      {/if}
    </div>

    <div class="bar">
      <div class="segments" aria-hidden="true">
        {#each turnSegments as segment (segment.turnNumber)}
          <span
            class="segment"
            class:current={segment.turnNumber === currentTurn}
            style={`left: ${segment.leftPct}%; width: ${segment.widthPct}%; --fill: ${segment.fillPct}%`}
          ></span>
        {/each}
      </div>

      <!-- Two paths for the whole run's pace rather than a node per bucket, and
           inside the bar rather than beside it: the lane's 18px are already
           spoken for, and the bar's existing hover growth is what turns the
           ribbon from a texture into something with amplitude to read. -->
      {#if ridge}
        <svg class="ridge" viewBox="0 0 100 1" preserveAspectRatio="none" aria-hidden="true">
          <path class="ridge-area" d={`${ridge}V1H0Z`} />
          <path class="ridge-line" d={ridge} />
        </svg>
      {/if}
    </div>

    <!-- The drag range is the lane as drawn, not the live total: against a total
         that grew while the scale was held, a drop would land some events away
         from where it was released. -->
    <input
      class="scrub-input"
      aria-label="Replay position"
      aria-valuetext={positionText}
      type="range"
      min="0"
      max={scale}
      value={viewIndex}
      oninput={handleInput}
    />

    <!-- Anomalies keep their own marks: they are the reason to scrub at all.
         No `title`: a native tooltip needs the pointer to rest on one element
         for about a second, which never happened while the marks were moving,
         and the lane's own label says it sooner and in our own type. -->
    {#each cues as marker (marker.index)}
      <button
        type="button"
        class={`cue ${marker.tone}`}
        class:aimed={aimedIndex === marker.index}
        style={`left: ${(marker.index / scale) * 100}%`}
        aria-label={`Jump to ${marker.label}, turn ${marker.turnNumber}`}
        onpointerenter={() => (aimedIndex = marker.index)}
        onpointerleave={() => (aimedIndex = null)}
        onfocus={() => (aimedIndex = marker.index)}
        onblur={() => (aimedIndex = null)}
        onclick={() => onScrub(marker.index)}
      ></button>
    {/each}

    <div class="playhead" style={`left: ${cursorPct}%`} aria-hidden="true">
      <span class="knob"></span>
    </div>
  </div>

  <div class="now">
    <Chip
      class="readout"
      active={detailOpen}
      disabled={!hasDetail}
      aria-expanded={detailOpen}
      aria-controls="now-card"
      data-tip={hasDetail
        ? `${eventTitle} — click for details (stays open while scrubbing)`
        : eventTitle}
      data-tip-align="end"
      onclick={() => (detailOpen = !detailOpen)}
    >
      {#snippet lead()}
        <span class={`dot ${tone}`}></span>
      {/snippet}
      <!-- Against the held scale, and holding room for the widest reading it can
           reach. The digits are tabular but their count is not: one more of them
           widens this chip, the lane is the flex item that pays for it, and
           every mark a hand is reaching for slides. Reserving the room here is
           what makes the lane's width, and so its aim, hold still. -->
      <span class="pos" style={`--pos-chars: ${String(scale).length * 2 + 1}`}
        >{shownIndex}/{scale}</span>
    </Chip>

    {#if detailOpen && hasDetail}
      <div
        class="now-card"
        id="now-card"
        {@attach dismissable({ ondismiss: () => (detailOpen = false), keep: ".step-controller" })}
      >
        <div class="now-head">
          <span class={`dot ${tone}`}></span>
          <strong>{eventTitle}</strong>
          {#if eventType}
            <span class="kicker now-type">{eventType}</span>
          {/if}
        </div>
        <p class="kicker now-where">
          {#if currentTurn != null}turn {currentTurn} ·{/if}
          event {viewIndex} of {total}
        </p>
        {#if eventBody}
          <p class="now-body">{eventBody}</p>
        {/if}
      </div>
    {/if}
  </div>

  <!-- The drawer's whole chrome. It replaces the pane header down there, which is
       why it is a real toggle with a pressed state rather than an opener: it is
       the only thing that says whether the drawer is holding anything, and the
       only way to shut it that does not involve dragging.

       Last, behind the readout, because that is how every other strip in this
       console orders itself — a pane header puts its name first and its icons
       against the trailing edge, the minimap puts the run first and its launcher
       last. It also puts the drawer's control in the window's corner, which is
       the one target a pointer cannot overshoot. -->
  <div class="aside">
    <IconButton
      label={drawerOpen ? "Close the bottom drawer" : "Open the bottom drawer"}
      tip={drawerOpen ? "Close the bottom drawer" : "Open the bottom drawer — latency trace"}
      pressed={drawerOpen}
      onclick={onToggleDrawer}
    >
      <PanelBottom size={14} />
    </IconButton>
  </div>
</footer>

<style>
  .step-controller {
    position: relative;
    z-index: 5;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-md);
    padding: var(--gap-md) var(--gutter);
    border-top: 1px solid var(--border);
    background: color-mix(in srgb, var(--surface-1) 92%, black);
  }

  .transport {
    flex: none;
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
  }

  /* Divides the movements from the one control that is not one. */
  .rule {
    align-self: stretch;
    width: 1px;
    background: var(--border);
  }

  /* Chip carries the box, the height, the press and the hover. Only the
     numerals need saying. */
  :global(.speed) {
    min-width: 42px;
    justify-content: center;
    font-variant-numeric: tabular-nums;
    letter-spacing: var(--letter-tight);
  }

  /* A fixed-height lane holds the bar, so growing it on hover shifts nothing. */
  .scrub {
    position: relative;
    flex: 1 1 240px;
    min-width: 160px;
    height: 18px;
    display: flex;
    align-items: center;
  }

  .bar {
    position: relative;
    width: 100%;
    height: 6px;
    background: var(--surface-0);
    box-shadow: inset 0 0 0 1px var(--border);
    transition: height var(--duration-fast) var(--ease-out);
  }

  .scrub:focus-within .bar {
    height: 12px;
  }

  .segments {
    position: absolute;
    inset: 0;
    overflow: hidden;
  }

  .segment {
    position: absolute;
    top: 0;
    bottom: 0;
    background: linear-gradient(
      to right,
      color-mix(in srgb, var(--text-2) 40%, transparent) 0 var(--fill),
      rgb(255 255 255 / 7%) var(--fill) 100%
    );
    /* Translucent, so the seam composites lighter than whatever it sits on and a
       turn boundary reads the same in the played part of the track as in the part
       still ahead. An opaque divider in --surface-0 was the bar's own background
       colour, so it only showed where the fill behind it happened to be light. */
    border-right: 1px solid var(--border-strong);
  }

  .segment.current {
    background: linear-gradient(
      to right,
      color-mix(in srgb, var(--text-1) 62%, transparent) 0 var(--fill),
      rgb(255 255 255 / 12%) var(--fill) 100%
    );
  }

  /* One muted tone, and no `z-index`: colour on this lane means an anomaly, and
     painting before the input above it is what keeps the ribbon from taking a
     click meant for a cue. The SVG clips to its own box, so a peak stays inside
     the bar instead of riding out over the transport. */
  .ridge {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }

  .ridge-area {
    fill: color-mix(in srgb, var(--text-1) 13%, transparent);
  }

  /* Non-scaling, because the box is one user unit tall and a plain stroke-width
     would be scaled to the height of the bar. */
  .ridge-line {
    fill: none;
    stroke: color-mix(in srgb, var(--text-1) 38%, transparent);
    stroke-width: 1;
    vector-effect: non-scaling-stroke;
  }

  /* The input owns the whole lane so the drag target is bigger than the bar. */
  .scrub-input {
    position: absolute;
    inset: 0;
    z-index: 1;
    width: 100%;
    height: 100%;
    margin: 0;
    padding: 0;
    background: transparent;
    appearance: none;
    -webkit-appearance: none;
    cursor: grab;
  }

  .scrub-input:active {
    cursor: grabbing;
  }

  .scrub-input:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 1px;
  }

  /* A hairline thumb keeps the pointer aligned with the painted playhead. */
  .scrub-input::-webkit-slider-thumb {
    appearance: none;
    -webkit-appearance: none;
    width: 2px;
    height: 18px;
    background: transparent;
    border: 0;
  }

  .scrub-input::-moz-range-thumb {
    width: 2px;
    height: 18px;
    background: transparent;
    border: 0;
  }

  /* Small mark, generous hit box: the tick is 3px, the button is 11px. */
  .cue {
    position: absolute;
    top: 0;
    z-index: 2;
    width: 11px;
    height: 100%;
    padding: 0;
    border: 0;
    background: transparent;
    transform: translateX(-50%);
    cursor: pointer;
  }

  .cue::before {
    content: "";
    position: absolute;
    top: 50%;
    left: 50%;
    width: 3px;
    height: 10px;
    background: var(--text-3);
    transform: translate(-50%, -50%);
    transition: height var(--duration-fast) var(--ease-out);
  }

  .cue.approval::before {
    background: var(--queue);
  }

  .cue.error::before {
    background: var(--error);
  }

  .cue.queue::before {
    background: var(--warning);
  }

  .cue:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -1px;
  }

  /* Whichever mark the label is currently describing stands slightly taller, so
     the tie between the two is visible with a pointer or a keyboard. */
  .cue.aimed::before {
    height: 14px;
  }

  .playhead {
    position: absolute;
    top: 0;
    z-index: 3;
    height: 100%;
    width: 2px;
    margin-left: -1px;
    background: var(--text-1);
    pointer-events: none;
  }

  .knob {
    position: absolute;
    top: 50%;
    left: 50%;
    width: 9px;
    height: 9px;
    background: var(--text-1);
    transform: translate(-50%, -50%) scale(0);
    transition: transform var(--duration-fast) var(--ease-out);
  }

  .scrub:focus-within .knob {
    transform: translate(-50%, -50%) scale(1);
  }

  .tip {
    position: absolute;
    bottom: calc(100% + var(--gap-sm));
    z-index: 20;
    display: flex;
    align-items: center;
    gap: 6px;
    max-width: min(340px, 100%);
    padding: 3px 8px;
    border: 1px solid var(--border-strong);
    background: var(--surface-3);
    box-shadow: var(--shadow-floating);
    color: var(--text-1);
    font-size: var(--font-sm);
    white-space: nowrap;
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--duration-fast) var(--ease-out);
  }

  .scrub:focus-within .tip {
    opacity: 1;
  }

  .tip-title {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .tip-turn {
    flex: none;
    color: var(--text-3);
  }

  .tip-hint {
    flex: none;
    color: var(--accent);
  }

  .dot {
    flex: none;
    width: var(--pip);
    height: var(--pip);
    background: var(--text-4);
  }

  .dot.error {
    background: var(--error);
  }

  .dot.approval,
  .dot.queue {
    background: var(--queue);
  }

  .dot.done {
    background: var(--success);
  }

  /* Holds the drawer's handle, and used to hold the token chip beside it. The
     chip went when tokens became a pane of their own, so this is back to one
     item — the gap and the rule that separated them went with it. */
  .aside {
    flex: none;
    display: flex;
    align-items: center;
  }

  .now {
    position: relative;
    flex: none;
  }

  .pos {
    min-width: calc(var(--pos-chars, 7) * 1ch);
    font-variant-numeric: tabular-nums;
    letter-spacing: var(--letter-tight);
  }

  /* Grows out of the readout rather than fading in from nowhere. */
  .now-card {
    position: absolute;
    right: 0;
    bottom: calc(100% + var(--gap-sm));
    z-index: 20;
    width: min(340px, 70vw);
    display: grid;
    gap: 6px;
    padding: 9px 10px;
    border: 1px solid var(--border-strong);
    background: var(--surface-3);
    box-shadow: var(--shadow-floating);
    transform-origin: bottom right;
    opacity: 1;
    transform: none;
    transition:
      opacity var(--duration-fast) var(--ease-out),
      transform var(--duration-fast) var(--ease-out);
  }

  @starting-style {
    .now-card {
      opacity: 0;
      transform: scale(0.96) translateY(3px);
    }
  }

  .now-head {
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }

  .now-head strong {
    min-width: 0;
    color: var(--text-1);
    font-size: var(--font-md);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .now-type {
    flex: none;
    margin-left: auto;
    color: var(--text-4);
  }

  .now-where {
    margin: 0;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }

  .now-body {
    margin: 0;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.4;
    overflow: hidden;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 4;
    line-clamp: 4;
  }

  @media (hover: hover) and (pointer: fine) {
    .scrub:hover .bar {
      height: 12px;
    }

    .scrub:hover .knob {
      transform: translate(-50%, -50%) scale(1);
    }

    .scrub:hover .tip {
      opacity: 1;
    }

    .cue:hover::before {
      height: 14px;
    }
  }

  @media (max-width: 760px) {
    .scrub {
      order: 3;
      flex: 1 1 100%;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .bar,
    .knob,
    .tip,
    .cue::before,
    .now-card {
      transition: none;
    }

    @starting-style {
      .now-card {
        opacity: 0;
        transform: none;
      }
    }
  }
</style>
