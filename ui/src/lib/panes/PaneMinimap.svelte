<script lang="ts">
  /**
   * The console's status line: what session you are in, what is open, and where
   * you are in it.
   *
   * The map is the part that was here first, and it is a minimap of the rail
   * rather than a trail of where you have been. Breadcrumbs are history and they
   * cost a chip per step, so a desk with a dozen panes on it either wraps the
   * chrome or truncates the names. This is the other object: one tick per pane in
   * rail order and a marker on the one you are in. It costs the same at forty
   * panes as at four.
   *
   * The session anchor arrives as `lead` because the row it needs is this one:
   * folding it in here is what let the app's third chrome band go, and the two
   * that are left each answer one question — this one what you are looking at,
   * the transport where in the run you are looking from.
   */
  import type { Snippet } from "svelte";
  import { Plus } from "@lucide/svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import type { PaneDescription } from "$lib/panes/PaneRail.svelte";
  import { PANE_KINDS, PANE_META, type PaneKind } from "$lib/panes/registry";
  import { dismissable } from "$lib/state/dismissable.svelte";
  import {
    activeIn,
    isSplit,
    slotKey,
    type Pane,
    type PaneStack
  } from "$lib/state/paneStack.svelte";

  interface Props {
    stack: PaneStack;
    describe: (pane: Pane) => PaneDescription;
    /** Whatever anchors the row ahead of the map — in the app, the session menu. */
    lead?: Snippet;
    /** Whatever closes the row after the launcher — in the app, the shortcuts hint. */
    trail?: Snippet;
  }

  let { stack, describe, lead, trail }: Props = $props();

  /**
   * The only hues that reach a tick.
   *
   * A row this dense is the worst place in the app to spend colour on identity:
   * the pane kinds outnumber the semantic hues, so a magenta "tool" tick would
   * sit next to a magenta "needs you" tick meaning something else entirely. So
   * the tick says what a pane is *doing*, and only when that is something a
   * person has to act on — waiting on an approval, retrying, or failed. Every
   * other pane, whatever kind it is, draws in the neutral ramp. One coloured
   * tick in a grey row is worth more than ten colour-coded ones.
   */
  const ATTENTION_TONES = new Set(["--live", "--error", "--retry"]);

  let ticksElement = $state<HTMLElement | null>(null);
  /** Where to draw the "you are here" box, in px within the tick run. */
  let marker = $state<{ x: number; width: number } | null>(null);

  const closable = $derived(PANE_KINDS.filter((kind) => !stack.has(kind)));

  let launcherOpen = $state(false);
  /* The marker is one box that slides between cells rather than an outline that
     blinks on and off, so it has to be measured: cells share a budget, so their
     width is whatever is left after the row is laid out, not a constant. */
  $effect(() => {
    const host = ticksElement;
    const currentId = stack.focusedId;
    /* Adding, closing, folding, or stacking a pane re-lays the run. */
    void stack.panes.length;
    void stack.groups.length;
    if (!host) return;

    const measure = (): void => {
      const cell = currentId
        ? host.querySelector<HTMLElement>(`[data-pane-tick="${CSS.escape(currentId)}"]`)
        : null;
      marker = cell ? { x: cell.offsetLeft, width: cell.offsetWidth } : null;
    };

    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  });

  function attentionTone(tone: string | null | undefined): string | null {
    return tone && ATTENTION_TONES.has(tone) ? tone : null;
  }

  /* A tick is a few pixels of colour, so everything it stands for has to be in
     the label the pointer and the screen reader get. */
  function tickLabel(pane: Pane, description: PaneDescription): string {
    return [
      PANE_META[pane.kind].kindLabel,
      description.title,
      description.statusLabel,
      pane.collapsed ? "folded" : null
    ]
      .filter(Boolean)
      .join(" · ");
  }

  /* `aria-disabled` rather than `disabled`, so the one state where the button
     does nothing is also the one state where its tip explains why — a disabled
     button takes no pointer events, so the hint never appears. The guard is
     what `disabled` was doing for free. */
  function toggleLauncher(): void {
    if (closable.length === 0) return;
    launcherOpen = !launcherOpen;
  }

  function openKind(kind: PaneKind): void {
    launcherOpen = false;
    stack.openPane({ kind }, stack.focusedId);
  }

  /* Escape and press-outside are the shared attachment's, on the popover itself; `keep` is
     the wrapper so the `+` that opened it can shut it again on one press rather than
     racing its own toggle. */
</script>

<nav class="minimap" aria-label="Session and open panes">
  <div class="minimap-lead">
    {#if lead}
      {@render lead()}
    {/if}
  </div>

  <div class="minimap-mark">
    <!-- Silent decoration. The document title names the console; nothing on this
         strip restates it. -->
    <svg class="brand-mark" viewBox="0 0 192 192" aria-hidden="true">
      <path
        fill="currentColor"
        d="M123.34 68.6596C119.655 41.0484 110.327 18 96 18C81.6731 18 72.3454 41.0484 68.6596 68.6596C41.0484 72.3454 18 81.6731 18 96C18 110.327 41.0525 119.655 68.6596 123.34C72.3454 150.948 81.6731 174 96 174C110.327 174 119.655 150.948 123.34 123.34C150.952 119.655 174 110.327 174 96C174 81.6731 150.948 72.3454 123.34 68.6596ZM67.7583 115.298C41.3151 111.479 25.893 102.737 25.893 96C25.893 89.2629 41.3151 80.5212 67.7583 76.7021C67.1764 83.0674 66.8733 89.566 66.8733 96C66.8733 102.434 67.1764 108.937 67.7583 115.298ZM96 25.893C102.737 25.893 111.479 41.3151 115.298 67.7583C108.937 67.1764 102.434 66.8733 96 66.8733C89.566 66.8733 83.0633 67.1764 76.7021 67.7583C80.5212 41.3151 89.2629 25.893 96 25.893ZM124.242 115.298C122.94 115.488 117.602 116.114 116.252 116.248C116.118 117.602 115.488 122.936 115.302 124.238C111.483 150.681 102.741 166.103 96.0041 166.103C89.267 166.103 80.5253 150.681 76.7061 124.238C76.5202 122.936 75.8898 117.598 75.7564 116.248C75.1421 109.979 74.7703 103.246 74.7703 96C74.7703 88.7537 75.1421 82.0206 75.7564 75.7483C82.0247 75.134 88.7577 74.7622 96.0041 74.7622C103.25 74.7622 109.983 75.134 116.252 75.7483C117.606 75.8817 122.94 76.5121 124.242 76.698C150.685 80.5172 166.111 89.2629 166.111 95.996C166.111 102.729 150.685 111.479 124.242 115.298Z"
      />
    </svg>
  </div>

  <div class="minimap-trail">
    <div
      class="ticks"
      bind:this={ticksElement}
      style={`--tick-count: ${stack.groups.length}`}
    >
      <ol class="tick-run">
        {#each stack.groups as group (slotKey(group))}
          {@const shared = group.length > 1}
          {@const tabbed = shared && !isSplit(group)}
          {@const front = activeIn(group)}
          <li
            class="tick-group"
            class:shared
            class:folded={group.every((pane) => pane.collapsed)}
          >
            {#each group as pane (pane.id)}
              {@const description = describe(pane)}
              {@const attention = attentionTone(description.statusTone)}
              <!-- Dimmed only behind a tab strip, where the pane really is out of
                   sight. In a split it is on screen, so it reads at full strength. -->
              <button
                type="button"
                class="tick"
                class:folded={pane.collapsed}
                class:behind={tabbed && pane.id !== front.id}
                data-pane-tick={pane.id}
                aria-current={stack.focusedId === pane.id ? "true" : undefined}
                aria-label={tickLabel(pane, description)}
                title={tickLabel(pane, description)}
                onclick={() => stack.expand(pane.id)}
              >
                <span
                  class="mark"
                  style={attention ? `background: var(${attention})` : undefined}
                ></span>
              </button>
            {/each}
          </li>
        {/each}
      </ol>

      {#if marker}
        <span
          class="here"
          aria-hidden="true"
          style={`--here-x: ${marker.x}px; --here-w: ${marker.width}px`}
        ></span>
      {/if}
    </div>

    <!-- Nothing is written between the run and the controls. The pane you are in
         names itself, in its own header or along its spine, and the marker on the
         run says which one it is; how many are open and how many are folded is the
         run's own shape — one mark per pane, half height for a folded one. A line
         of prose restating either is how the name of the agent ended up on screen
         three times.

         Trail holds ticks then controls; flex-end on the trail keeps them on the
         right edge. No gap: a pane header's controls are 20px boxes 4px apart, and
         these are 28px boxes, so abutting them puts the same air between the glyphs.
         The larger box is kept for the hit area — the strip below is the visual
         reference, not the 20px target. -->
    <div class="controls">
      <div class="launcher">
        <IconButton
          class="rail-icon"
          label="Open a view"
          tip={closable.length === 0 ? "Every view is already open" : "Open a view"}
          aria-expanded={launcherOpen}
          aria-disabled={closable.length === 0 ? "true" : undefined}
          data-tip-below
          data-tip-align="end"
          onclick={toggleLauncher}
        >
          <Plus size={13} />
        </IconButton>

        {#if launcherOpen}
          <div
            class="launch-menu"
            role="menu"
            aria-label="Open a view"
            {@attach dismissable({ ondismiss: () => (launcherOpen = false), keep: ".launcher" })}
          >
            {#each closable as kind (kind)}
              <button type="button" role="menuitem" onclick={() => openKind(kind)}>
                <span class="kicker">{PANE_META[kind].kindLabel}</span>
              </button>
            {/each}
          </div>
        {/if}
      </div>

      {#if trail}
        {@render trail()}
      {/if}
    </div>
  </div>
</nav>

<style>
  /* One row of --control-height controls inset by --gutter-tight, which is what
     the transport under the rail is, so the two strips frame the panes at the
     same weight. Must not clip: the launcher popover hangs below it. */
  .minimap {
    flex: none;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    column-gap: var(--gutter);
    padding: var(--gutter-tight) var(--gutter);
    border-bottom: 1px solid var(--border);
    background: var(--surface-head);
  }

  .minimap-lead {
    min-width: 0;
    display: flex;
    align-items: center;
    justify-self: start;
  }

  .minimap-mark {
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .minimap-trail {
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--gutter);
    justify-self: stretch;
  }

  /* Solid fill outweighs a stroke glyph at the same size, so this sits under the
     28px control boxes rather than filling them — large enough to hold the row,
     quiet enough not to outrank the 13px rail icons. */
  .brand-mark {
    flex: none;
    width: 20px;
    height: 20px;
    color: var(--text-3);
    pointer-events: none;
    user-select: none;
  }

  /* One pip of ink and one pip of air per pane. Stating the pitch in the ink is
     what keeps the run reading as a strip: at a 12px pitch a 4px mark would leave
     twice its own width of air on either side, and a row of pins is harder to
     scan than a row of bars.
   *
     Constant space is the whole point, so the run is a fixed budget the ticks
     share rather than a list that grows. Up to twenty slots each tick gets the
     full pitch; past that the run stops widening and the pitch divides instead, so
     forty slots cost exactly what twenty did and a hundred still fit — the ticks
     just close up into a bar with the marker still on it, which is the honest
     reading of a hundred panes anyway. Stating the budget in ticks rather than
     pixels keeps the two ends of the ramp tied together. Panes sharing a column
     share one slot of the run, stacked the way the column is. */
  .ticks {
    --tick-pitch: calc(2 * var(--pip));
    --run-budget: calc(20 * var(--tick-pitch));
    position: relative;
    flex: 0 1 auto;
    width: min(var(--run-budget), calc(var(--tick-count) * var(--tick-pitch)));
    min-width: 0;
    height: var(--control-height);
  }

  .tick-run {
    display: flex;
    align-items: stretch;
    height: 100%;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .tick-group {
    display: flex;
    flex: 1 1 0;
    min-width: 0;
    align-items: stretch;
  }

  /* Panes share a cell the way they share a column, so a desk of eight panes in
     four columns still reads as four places to be.
   *
   * The stack is the same bar as its neighbours, cut by a hairline — not a pair of
   * short dashes with air between them. Centring each mark in its own slice put
   * 8px between them, the same gap that separates two different columns, so a
   * shared column read as two half-panes instead of one place holding two. The
   * band is pinned to the height of a single mark and the slices divide it. */
  .tick-group.shared {
    --band: var(--gap-lg);
    --band-inset: calc((var(--control-height) - var(--band)) / 2);
    flex-direction: column;
    gap: 1px;
    box-sizing: border-box;
    padding: var(--band-inset) 0;
  }

  /* Folded, the whole column is one spine, so its band is the half-height a folded
     pane's mark has always been. */
  .tick-group.shared.folded {
    --band: var(--gap-sm);
  }

  .tick-group.shared .tick {
    position: relative;
  }

  /* The ink shrank to the band, so the hit area is handed back: the outer slices
     reach the full height of the run again, and the boundary between them stays on
     the seam a reader is aiming at. */
  .tick-group.shared .tick:first-child::before,
  .tick-group.shared .tick:last-child::before {
    content: "";
    position: absolute;
    inset: 0;
  }

  .tick-group.shared .tick:first-child::before {
    top: calc(-1 * var(--band-inset));
  }

  .tick-group.shared .tick:last-child::before {
    bottom: calc(-1 * var(--band-inset));
  }

  /* A slice is thinner than the ring drawn inside it, so the ring goes outside. */
  .tick-group.shared .tick:focus-visible {
    outline-offset: 1px;
  }

  /* The hit area is the cell, not the mark: the full height of the bar and the
     full pitch wide, with cells abutting so a near miss lands on the neighbour
     rather than on nothing. Enlarging the mark itself would turn the map into
     a row of buttons. */
  .tick {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: pointer;
  }

  .tick:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  /* A pip wide where there is room, and always a hairline short of its slot: past
     twenty panes the slots divide below the pip and the marks would meet, leaving a
     solid bar with no way to tell forty panes from a hundred. */
  .mark {
    width: calc(100% - 1px);
    max-width: var(--pip);
    height: var(--gap-lg);
    background: var(--text-3);
    transition:
      height var(--duration-fast) var(--ease-out),
      background var(--duration-fast) var(--ease-out);
  }

  /* A folded pane is still in the rail as a 42px spine, so its tick is the same
     mark at half height rather than a different colour or a gap. Height reads
     as "less of it", which is what folding did; colour is spoken for. */
  .tick.folded .mark {
    height: var(--gap-sm);
    background: var(--text-4);
  }

  /* The slices divide the band, so the marks fill them: two panes in a column are
     two halves of one bar, three are thirds of it. */
  .tick-group.shared .mark,
  .tick-group.shared .tick.folded .mark {
    height: 100%;
  }

  /* A tab nobody can see is still open, and still one click away, so it stays on
     the map — just quieter than the one its column is showing. */
  .tick.behind .mark {
    background: var(--text-4);
  }

  .tick[aria-current] .mark {
    background: var(--text-1);
  }

  /* One box that slides, so moving focus across the desk shows you where it
     went. Mounted at its measured place, so it does not fly in from the left
     on first paint. */
  .here {
    position: absolute;
    top: 0;
    left: 0;
    width: var(--here-w);
    height: 100%;
    border: 1px solid var(--accent);
    pointer-events: none;
    transform: translateX(var(--here-x));
    transition:
      transform var(--duration-fast) var(--ease-out),
      width var(--duration-fast) var(--ease-out);
  }

  .controls {
    flex: none;
    display: flex;
    align-items: center;
  }

  /* Bare at rest and boxed on hover, which is what a pane header's pin, collapse
     and close do. The primitive still owns the size, the hover, the press, the
     focus ring and the disabled dimming; the only thing taken off it is the
     resting box, which is what makes one icon a control and three icons a row
     of buttons. `:global` because the class rides across a component boundary —
     the shortcuts button arrives through `trail`, from App. */
  .minimap :global(.rail-icon) {
    border-color: transparent;
    background: transparent;
  }

  /* Same "this one is on" as a pane header's pinned button. */
  .minimap :global(.rail-icon[aria-expanded="true"]) {
    border-color: var(--border-strong);
    color: var(--text-1);
  }

  .launcher {
    position: relative;
    flex: none;
  }

  /* Grows out of the button it came from rather than fading in from nowhere.
     Chrome popovers sit in the 40s so they clear every pane-level overlay,
     which tops out at 30. */
  .launch-menu {
    position: absolute;
    top: calc(100% + var(--gap-xs));
    right: 0;
    z-index: 44;
    display: flex;
    flex-direction: column;
    min-width: 150px;
    padding: var(--gap-xs);
    border: 1px solid var(--border-strong);
    background: var(--surface-3);
    transform-origin: top right;
    opacity: 1;
    transform: none;
    transition:
      opacity var(--duration-fast) var(--ease-out),
      transform var(--duration-fast) var(--ease-out);
  }

  @starting-style {
    .launch-menu {
      opacity: 0;
      transform: scale(0.96) translateY(-3px);
    }
  }

  .launch-menu button {
    display: flex;
    align-items: center;
    height: var(--control-height);
    padding: 0 var(--gap-md);
    border: 0;
    background: transparent;
    color: var(--text-2);
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .launch-menu button:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  @media (hover: hover) and (pointer: fine) {
    .tick:hover .mark {
      background: var(--text-1);
    }

    .launch-menu button:hover {
      background: var(--control-hover);
      color: var(--text-1);
    }
  }

  /* Below the rail's minimum useful width the seams close up, and the centered
     mark is the one thing in the row that gives way — silent decoration nobody
     has to reach. */
  @media (max-width: 980px) {
    .minimap {
      column-gap: var(--gutter-tight);
    }
  }

  @media (max-width: 760px) {
    .minimap {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    }

    .minimap-mark {
      display: none;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .mark,
    .here,
    .launch-menu {
      transition: none;
    }

    @starting-style {
      .launch-menu {
        opacity: 0;
        transform: none;
      }
    }
  }
</style>
