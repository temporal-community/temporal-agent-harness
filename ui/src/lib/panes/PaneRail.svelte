<script lang="ts">
  import type { Snippet } from "svelte";
  import { flip } from "svelte/animate";
  import { cubicOut } from "svelte/easing";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import {
    dropEdgeAt,
    dropEdgeLabel,
    edgeShares,
    SPLIT_MIN,
    type PaneDropEdge
  } from "$lib/panes/paneDrop";
  import PaneShell from "$lib/panes/PaneShell.svelte";
  import { PANE_META, ROOT_KINDS, SPINE_SIZE } from "$lib/panes/registry";
  import {
    activeIn,
    isSplit,
    slotKey,
    type Pane,
    type PaneStack
  } from "$lib/state/paneStack.svelte";

  export interface PaneDescription {
    title: string;
    statusTone?: string | null;
    statusLabel?: string | null;
  }

  interface Props {
    stack: PaneStack;
    describe: (pane: Pane) => PaneDescription;
    paneContent: Snippet<[Pane]>;
    /** The pane filling the screen on its own; the rest of the rail stands down. */
    bleedingId?: string | null;
  }

  let { stack, describe, paneContent, bleedingId = null }: Props = $props();

  /* Long enough to read as one column overtaking another, short enough that a
     reader dropping three panes in a row is never waiting on it. */
  const SETTLE_MS = 220;

  let railElement = $state<HTMLElement | null>(null);
  let draggingId = $state<string | null>(null);
  /* The column and the pane the pointer is over, plus where the drop would land.
     Null means this drop would do nothing, and nothing is drawn — the rail never
     shows a landing it will not honour. */
  let dropSlot = $state<string | null>(null);
  let dropPane = $state<string | null>(null);
  let dropEdge = $state<PaneDropEdge | null>(null);
  let resizingShare = $state<string | null>(null);
  let resizingColumn = $state<string | null>(null);
  /* A pane that changes column is rebuilt by Svelte, and a rebuilt pane would
     play the open animation as if it had just been opened. The gate has to
     outlive the drop's own render, so it lifts a frame later. */
  let settling = $state(false);

  function reducedMotion(): boolean {
    if (typeof window === "undefined") return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function paneElement(id: string): HTMLElement | null {
    return (
      railElement?.querySelector<HTMLElement>(
        `[data-pane="${CSS.escape(id)}"] .pane, [data-pane="${CSS.escape(id)}"] .spine`
      ) ?? null
    );
  }

  function slotElement(key: string): HTMLElement | null {
    return railElement?.querySelector<HTMLElement>(`[data-slot="${CSS.escape(key)}"]`) ?? null;
  }

  /**
   * Step focus to the neighbouring pane and take DOM focus with it, so the
   * keyboard walk moves the caret and not just the highlight.
   */
  export function focusCurrent(): void {
    const id = stack.focusedId;
    if (id) paneElement(id)?.focus();
  }

  export function focusAdjacent(delta: number): void {
    stack.focusAdjacent(delta);
    focusCurrent();
  }

  /* Keep the focused pane on screen when it is opened from a pane far to the
     left, or when focus moves by keyboard. */
  $effect(() => {
    const id = stack.focusedId;
    if (!id) return;
    const target = paneElement(id);
    if (!target) return;
    target.scrollIntoView({
      behavior: reducedMotion() ? "instant" : "smooth",
      block: "nearest",
      inline: "nearest"
    });
  });

  function groupCollapsed(group: Pane[]): boolean {
    return group.length > 0 && group.every((pane) => pane.collapsed);
  }

  /* Tabs change what a column shows, not how much room it is owed: a column
     holding the graph still absorbs the leftover width, or the rail ends in dead
     space the moment anything is tabbed. */
  function groupFlexible(group: Pane[]): boolean {
    if (groupCollapsed(group)) return false;
    if (group.some((pane) => pane.size != null)) return false;
    return group.some((pane) => PANE_META[pane.kind].flexible);
  }

  function slotStyle(group: Pane[]): string {
    /* Folded columns publish the spine size into the same vars the open column
       uses, so a leftover `--slot-min` from the open pane (graph is 420) cannot
       outvote the collapsed flex basis if anything else reads it. */
    if (groupCollapsed(group)) {
      return [
        `--slot-size: ${SPINE_SIZE}px`,
        `--slot-min: ${SPINE_SIZE}px`,
        `--sticky-offset: ${stack.stickyOffsetFor(group[0].id)}px`
      ].join("; ");
    }
    return [
      `--slot-size: ${stack.sizeOfGroup(group)}px`,
      `--slot-min: ${stack.minOfGroup(group)}px`,
      `--sticky-offset: ${stack.stickyOffsetFor(group[0].id)}px`
    ].join("; ");
  }

  function startDrag(event: DragEvent, pane: Pane): void {
    draggingId = pane.id;
    clearDrop();
    /* Firefox refuses to start a drag without payload. */
    event.dataTransfer?.setData("text/plain", pane.id);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
  }

  function elementFromEvent(event: DragEvent): Element | null {
    const node = event.target;
    return node instanceof Element
      ? node
      : node instanceof Node
        ? node.parentElement
        : null;
  }

  function groupFor(key: string): Pane[] | null {
    return stack.groups.find((group) => slotKey(group) === key) ?? null;
  }

  function clearDrop(): void {
    dropSlot = null;
    dropPane = null;
    dropEdge = null;
  }

  function updateDrop(event: DragEvent): void {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    if (!draggingId) return;

    const element = elementFromEvent(event);
    const paneBox = element?.closest<HTMLElement>("[data-pane]");
    const key = element?.closest("[data-slot]")?.getAttribute("data-slot") ?? null;
    const paneId = paneBox?.getAttribute("data-pane") ?? null;
    const group = key ? groupFor(key) : null;
    const column = key ? slotElement(key) : null;
    if (!key || !paneBox || !paneId || !group || !column) {
      clearDrop();
      return;
    }

    const edge = dropEdgeAt(
      event.clientX,
      event.clientY,
      column.getBoundingClientRect(),
      paneBox.getBoundingClientRect()
    );
    /* Alone in its column, a dragged pane is already everywhere this column could
       put it, and joining a column you are already a tab of is where you are. */
    const holds = group.some((pane) => pane.id === draggingId);
    if (holds && (group.length === 1 || (edge === "tab" && !isSplit(group)))) {
      clearDrop();
      return;
    }
    dropSlot = key;
    dropPane = paneId;
    dropEdge = edge;
  }

  function handleDrop(event: DragEvent): void {
    event.preventDefault();
    if (draggingId && dropPane && dropEdge) {
      stack.placePane(draggingId, dropPane, dropEdge);
    }
    endDrag();
  }

  function handleDragLeave(event: DragEvent): void {
    const next = event.relatedTarget;
    if (next instanceof Node && railElement?.contains(next)) return;
    clearDrop();
  }

  function endDrag(): void {
    if (draggingId) {
      settling = true;
      requestAnimationFrame(() => {
        settling = false;
      });
    }
    draggingId = null;
    clearDrop();
  }

  /** Edges drawn against the column rather than against one pane inside it. */
  function edgeInColumn(edge: PaneDropEdge): boolean {
    return edge === "before" || edge === "after" || edge === "tab";
  }

  function resizeColumnFrom(event: PointerEvent, group: Pane[]): void {
    const slot = (event.currentTarget as HTMLElement).parentElement;
    const rect = slot?.getBoundingClientRect();
    if (!rect) return;
    stack.setGroupSize(group[0].id, Math.round(event.clientX - rect.left));
  }

  function startColumnResize(event: PointerEvent, group: Pane[]): void {
    if (event.button !== 0 && event.pointerType !== "touch") return;
    event.preventDefault();
    resizingColumn = slotKey(group);
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    resizeColumnFrom(event, group);
  }

  function moveColumnResize(event: PointerEvent, group: Pane[]): void {
    if (resizingColumn === slotKey(group)) resizeColumnFrom(event, group);
  }

  function stopColumnResize(event: PointerEvent): void {
    resizingColumn = null;
    const handle = event.currentTarget as HTMLElement;
    if (handle.hasPointerCapture(event.pointerId)) {
      handle.releasePointerCapture(event.pointerId);
    }
  }

  /* The seam inside a split column. Measured from the pane's own top rather than
     the column's, so a three-way split resizes the pane above the seam being
     dragged and leaves the ones over it alone. */
  function resizeShareFrom(event: PointerEvent, pane: Pane): void {
    const box = (event.currentTarget as HTMLElement).parentElement;
    const rect = box?.getBoundingClientRect();
    if (!rect) return;
    stack.setShare(pane.id, Math.max(SPLIT_MIN, Math.round(event.clientY - rect.top)));
  }

  function startShareResize(event: PointerEvent, pane: Pane): void {
    if (event.button !== 0 && event.pointerType !== "touch") return;
    event.preventDefault();
    resizingShare = pane.id;
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    resizeShareFrom(event, pane);
  }

  function moveShareResize(event: PointerEvent, pane: Pane): void {
    if (resizingShare === pane.id) resizeShareFrom(event, pane);
  }

  function stopShareResize(event: PointerEvent): void {
    resizingShare = null;
    const handle = event.currentTarget as HTMLElement;
    if (handle.hasPointerCapture(event.pointerId)) {
      handle.releasePointerCapture(event.pointerId);
    }
  }

  /* Tabs are a tablist, so arrows walk them; the rest of the desk is reached with
     Alt+Arrows, which the window handles. */
  function handleTabKeydown(event: KeyboardEvent, group: Pane[], index: number): void {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const next = index + (event.key === "ArrowLeft" ? -1 : 1);
    if (next < 0 || next >= group.length) return;
    event.preventDefault();
    stack.focusPane(group[next].id);
  }

  function tabPanelId(pane: Pane): string {
    return `pane-panel-${pane.id}`;
  }
</script>

<div
  class="rail"
  class:settling
  class:bleed={bleedingId != null}
  bind:this={railElement}
  style={`--pane-spine: ${SPINE_SIZE}px; --split-min: ${SPLIT_MIN}px`}
  role="region"
  aria-label="Pane rail"
  ondragover={updateDrop}
  ondrop={handleDrop}
  ondragleave={handleDragLeave}
>
  {#if stack.groups.length === 0}
    <div class="rail-empty">
      <p class="empty-title">Open space</p>
      <p class="empty-body">
        Every reference opens here. Drag a pane by its header: to the side of a
        column to move it there, to the top or bottom of one to split it, or onto
        the middle to keep both as tabs.
      </p>
      <div class="empty-actions">
        {#each ROOT_KINDS as kind (kind)}
          <Chip
            label={PANE_META[kind].kindLabel}
            size="xs"
            fill="quiet"
            onclick={() => stack.openPane({ kind })}
          />
        {/each}
      </div>
    </div>
  {:else}
    <!-- Keyed per slot, so a column that changes place has its nodes moved
         rather than torn down and built again — and `flip` animates the move
         from where it was, which is what makes sorting read as sorting. -->
    {#each stack.groups as group, groupIndex (slotKey(group))}
      {@const collapsed = groupCollapsed(group)}
      {@const split = isSplit(group)}
      {@const tabbed = group.length > 1 && !split}
      {@const front = activeIn(group)}
      <!-- Folded down, a column is a spine. Tabs behind the front one still have
           nothing to show, so a folded column is one surface. -->
      {@const shown = collapsed ? [front] : group}
      <div
        class="rail-slot"
        class:collapsed
        class:tabbed
        class:split={split && !collapsed}
        class:flexible={groupFlexible(group)}
        class:bleeding={group.some((pane) => pane.id === bleedingId)}
        class:dropping={dropSlot === slotKey(group)}
        style={slotStyle(group)}
        data-slot={slotKey(group)}
        data-group={groupIndex}
        animate:flip={{ duration: reducedMotion() ? 0 : SETTLE_MS, easing: cubicOut }}
      >
        {#if tabbed && !collapsed}
          <div class="tab-strip" role="tablist" aria-label="Panes in this column">
            {#each group as pane, tabIndex (pane.id)}
              {@const description = describe(pane)}
              {@const meta = PANE_META[pane.kind]}
              <button
                type="button"
                role="tab"
                class="tab"
                class:on={pane.id === front.id}
                aria-selected={pane.id === front.id}
                aria-controls={tabPanelId(pane)}
                tabindex={pane.id === front.id ? 0 : -1}
                title={description.statusLabel
                  ? `${description.title} — ${description.statusLabel}`
                  : description.title}
                draggable="true"
                data-tab={pane.id}
                ondragstart={(event) => startDrag(event, pane)}
                ondragend={endDrag}
                onclick={() => stack.focusPane(pane.id)}
                onkeydown={(event) => handleTabKeydown(event, group, tabIndex)}
              >
                <span
                  class="tab-pip"
                  style={`background: var(${description.statusTone ?? meta.accent})`}
                ></span>
                <span class="tab-label">{meta.kindLabel}</span>
              </button>
            {/each}
          </div>
        {/if}

        <div class="slot-body">
          {#each shown as pane, paneIndex (pane.id)}
            {@const description = describe(pane)}
            {@const isFront = pane.id === front.id}
            <!-- In a split every pane is on screen; behind a tab strip only the
                 front one is, and the rest stay mounted at their full size —
                 hiding them by display would make the graph remeasure at zero and
                 lose its zoom every time a reader flicked between tabs. -->
            {@const visible = split || isFront}
            <div
              class="pane-slot"
              class:front={visible}
              class:bleeding={pane.id === bleedingId}
              class:dragging={draggingId === pane.id}
              class:sized={split && pane.share != null}
              class:resizing={resizingShare === pane.id}
              style={split && pane.share != null ? `--share: ${pane.share}px` : undefined}
              id={tabPanelId(pane)}
              role={tabbed ? "tabpanel" : undefined}
              aria-hidden={visible ? undefined : "true"}
              inert={!visible}
              data-pane={pane.id}
            >
              <PaneShell
                {pane}
                shared={group.length > 1}
                {split}
                title={description.title}
                statusTone={description.statusTone ?? null}
                statusLabel={description.statusLabel ?? null}
                focused={stack.focusedId === pane.id}
                canClose={!pane.pinned}
                canResize={!split && isFront && !pane.collapsed}
                onFocus={() => stack.focusPane(pane.id)}
                onToggleCollapse={() => stack.toggleCollapse(pane.id)}
                onTogglePin={() => stack.togglePin(pane.id)}
                onClose={() => stack.closePane(pane.id)}
                onResize={(size) => stack.setGroupSize(pane.id, size)}
                onToggleSplit={() => stack.setSplit(pane.id, !split)}
                onDragStart={(event) => startDrag(event, pane)}
                onDragEnd={endDrag}
              >
                {#snippet content()}
                  {@render paneContent(pane)}
                {/snippet}
              </PaneShell>

              {#if split && paneIndex < shown.length - 1}
                <button
                  type="button"
                  class="share-gutter"
                  aria-label={`Resize ${description.title} within its column`}
                  title="Drag to move the split — double-click to share evenly"
                  onpointerdown={(event) => startShareResize(event, pane)}
                  onpointermove={(event) => moveShareResize(event, pane)}
                  onpointerup={stopShareResize}
                  onpointercancel={stopShareResize}
                  ondblclick={() => stack.setShare(pane.id, null)}
                ></button>
              {/if}

              {#if dropPane === pane.id && (dropEdge === "above" || dropEdge === "below")}
                <div class="drop-mark" data-edge={dropEdge} aria-hidden="true">
                  <span class="drop-label kicker">{dropEdgeLabel(dropEdge)}</span>
                </div>
              {/if}
            </div>
          {/each}
        </div>

        <!-- A split column is sized as a whole, so its width gutter runs the full
             height rather than belonging to whichever pane is on top. -->
        {#if split && !collapsed}
          <button
            type="button"
            class="col-gutter"
            aria-label="Resize this column"
            title="Drag to set the column width — double-click to reset"
            onpointerdown={(event) => startColumnResize(event, group)}
            onpointermove={(event) => moveColumnResize(event, group)}
            onpointerup={stopColumnResize}
            onpointercancel={stopColumnResize}
            ondblclick={() => stack.setGroupSize(group[0].id, null)}
          ></button>
        {/if}

        {#if dropSlot === slotKey(group) && dropEdge && edgeInColumn(dropEdge)}
          <div class="drop-mark" data-edge={dropEdge} aria-hidden="true">
            {#if dropEdge === "tab"}
              <span class="drop-label kicker">{dropEdgeLabel(dropEdge)}</span>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  {/if}
</div>

<style>
  /* One direction, always. Columns were the only arrangement readers wanted, and
     keeping a second one meant every rule in this file had a mirror image that
     had to be kept true. */
  .rail {
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: row;
    align-items: stretch;
    overflow-x: auto;
    overflow-y: hidden;
    background: var(--surface-0);
    scrollbar-width: thin;
  }

  .rail-slot {
    position: relative;
    display: flex;
    flex-direction: column;
    flex: 0 0 var(--slot-size);
    min-width: var(--slot-min);
    min-height: 0;
  }

  .rail-slot.flexible {
    flex: 1 1 var(--slot-size);
  }

  /* Full bleed: one column takes the rail and the others stand down. Display,
     not visibility, because a column left laid out would keep its width and
     leave the canvas short of it — and the panes inside are remeasured on the
     way back anyway, which is what the rail's own resize observers are for.

     The pane's chrome goes with the desk's. Its header is a drag handle and a
     row of controls for arranging a desk that is not on screen, and its width
     gutter has nothing left to size against. */
  .rail.bleed {
    overflow-x: hidden;
  }

  .rail.bleed .rail-slot:not(.bleeding) {
    display: none;
  }

  .rail.bleed .rail-slot.bleeding {
    position: static;
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
    max-width: none;
    border-right: 0;
  }

  /* A split column shows every pane in it, and a tab strip arranges a column
     that is no longer on screen. One pane bleeds, so neither survives. */
  .rail.bleed .pane-slot:not(.bleeding) {
    display: none;
  }

  .rail.bleed .rail-slot.bleeding .tab-strip,
  .rail.bleed .rail-slot.bleeding :global(.pane-head),
  .rail.bleed .rail-slot.bleeding :global(.resize-gutter) {
    display: none;
  }

  .rail.bleed .rail-slot.bleeding :global(.pane) {
    border-right: 0;
  }

  /* A shared column carries the outline a single pane would have carried, so a
     column of three tabs — or three panes split down it — is the same object on
     the rail as the column beside it holding one. */
  .rail-slot.tabbed,
  .rail-slot.split {
    border-right: 1px solid var(--border);
  }

  /* One edge, drawn once. A pane on its own draws its own right border; in a
     shared column the column draws it, and the panes inside must not, or the
     seam between columns lands at two pixels next to its neighbours' one. */
  .rail-slot.tabbed :global(.pane),
  .rail-slot.split :global(.pane) {
    border-right: 0;
  }

  .slot-body {
    position: relative;
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    min-height: 0;
  }

  .pane-slot {
    position: relative;
    display: flex;
    flex: 1 1 0;
    min-width: 0;
    min-height: 0;
  }

  /* Behind a tab strip the panes are stacked in one box, so each keeps the size it
     would have had in front and nothing remeasures on a tab change. In a split
     they sit in normal flow, sharing the column's height. */
  .rail-slot.tabbed .pane-slot {
    position: absolute;
    inset: 0;
  }

  .rail-slot.split .pane-slot {
    min-height: var(--split-min);
  }

  .rail-slot.split .pane-slot.sized {
    flex: 0 0 var(--share);
  }

  /* The seam between panes in a split. The pane above it draws it, so the last
     pane in the column leaves the column's own bottom edge alone. */
  .rail-slot.split .pane-slot:not(:last-child) {
    border-bottom: 1px solid var(--border);
  }

  .pane-slot:not(.front) {
    visibility: hidden;
    pointer-events: none;
  }

  .pane-slot.dragging {
    opacity: 0.55;
  }

  /* Sits astride the seam, and is invisible until pointed at — the same handle the
     pane shell puts on a column's edge, turned a quarter turn. A column split
     three ways would otherwise carry two permanent bars across the reading. */
  .share-gutter {
    position: absolute;
    inset: auto 0 -6px 0;
    z-index: 5;
    height: 12px;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: row-resize;
    touch-action: none;
    transition: background var(--duration-fast) var(--ease-out);
  }

  /* The width handle for a split column, running its whole height, because a
     column sized as one thing is grabbed as one thing. */
  .col-gutter {
    position: absolute;
    inset: 0 -6px 0 auto;
    z-index: 5;
    width: 12px;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: col-resize;
    touch-action: none;
    transition: background var(--duration-fast) var(--ease-out);
  }

  .pane-slot.resizing .share-gutter,
  .share-gutter:focus-visible,
  .col-gutter:focus-visible {
    background: color-mix(in srgb, var(--accent) 30%, transparent);
  }

  .share-gutter:focus-visible,
  .col-gutter:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -4px;
  }

  .tab-strip {
    flex: none;
    display: flex;
    align-items: stretch;
    gap: 1px;
    overflow-x: auto;
    background: var(--surface-0);
    border-bottom: 1px solid var(--border);
    scrollbar-width: none;
  }

  .tab {
    display: flex;
    align-items: center;
    gap: var(--gap-xs);
    min-width: 0;
    padding: 4px var(--gutter);
    border: 0;
    border-bottom: 1px solid transparent;
    background: transparent;
    color: var(--text-3);
    cursor: grab;
    transition:
      color var(--duration-fast) var(--ease-out),
      background var(--duration-fast) var(--ease-out);
  }

  .tab:active {
    cursor: grabbing;
  }

  .tab.on {
    background: var(--surface-head);
    border-bottom-color: var(--accent);
    color: var(--text-1);
  }

  .tab:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  .tab-pip {
    flex: none;
    width: var(--pip);
    height: var(--pip);
  }

  .tab-label {
    min-width: 0;
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    font-weight: var(--label-weight);
    letter-spacing: var(--label-tracking);
    text-transform: uppercase;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* The moved pane is rebuilt where it lands, and the entrance means "this pane
     is new". Held off for the frame the drop renders in, so the arrival is the
     column sliding over rather than a pane fading in on top of one. */
  .rail.settling :global(.pane),
  .rail.settling :global(.spine) {
    transition: none;
  }

  /* Spines accumulate against the leading edge as the rail scrolls: the move
     that makes a deep desk navigable. Width is locked — vertical labels must
     clip, not widen the column past the spine size. */
  .rail-slot.collapsed {
    position: sticky;
    left: var(--sticky-offset);
    z-index: 4;
    flex: 0 0 var(--pane-spine);
    width: var(--pane-spine);
    min-width: var(--pane-spine);
    max-width: var(--pane-spine);
    overflow: hidden;
  }

  /* Where the pane will land. A rule down the edge it will take, or a wash over
     the column it will join — the destination itself marked, rather than a card
     floating over the rail describing it. */
  .drop-mark {
    position: absolute;
    inset: 0;
    z-index: 6;
    pointer-events: none;
  }

  .drop-mark[data-edge="before"],
  .drop-mark[data-edge="after"] {
    width: 3px;
    background: var(--accent);
  }

  .drop-mark[data-edge="before"] {
    inset: 0 auto 0 -1px;
  }

  .drop-mark[data-edge="after"] {
    inset: 0 -1px 0 auto;
  }

  /* The three landings that put the pane inside the box you are pointing at
     preview the same way: the region it will occupy, washed and outlined.
     
     A split used to be a 3px rule along the pane's edge. A line is a seam, and
     a seam answers "where does the join go" — but the question being asked
     mid-drag is "how much of this do I get", and next to a tab landing that
     lights up the whole pane, a hairline read as a much smaller thing rather
     than a differently-shaped one. Half the pane lights up because half the
     pane is what a split hands over.
     
     Drawn on the pane the pointer is over rather than on the column, so in an
     already-split column it is the half of THAT pane which lights, not half of
     everything beside it. */
  .drop-mark[data-edge="above"],
  .drop-mark[data-edge="below"],
  .drop-mark[data-edge="tab"] {
    display: grid;
    place-items: start center;
    padding-top: 6px;
    background: color-mix(in srgb, var(--accent) 10%, transparent);
    box-shadow: inset 0 0 0 1px var(--accent);
  }

  .drop-mark[data-edge="above"],
  .drop-mark[data-edge="below"] {
    height: 50%;
  }

  .drop-mark[data-edge="above"] {
    inset: 0 0 auto 0;
  }

  /* Label at the top of the lit region either way, which for a bottom landing
     puts it on the seam — the edge the incoming pane starts at. */
  .drop-mark[data-edge="below"] {
    inset: auto 0 0 0;
  }

  .drop-label {
    padding: 0 var(--gap-xs);
    background: var(--accent);
    color: var(--surface-0);
  }

  .rail-empty {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    justify-content: center;
    align-items: center;
    flex: 1;
    padding: 32px;
    text-align: center;
  }

  .empty-title {
    margin: 0;
    color: var(--text-2);
    font-size: var(--font-2xl);
    font-weight: 450;
  }

  .empty-body {
    margin: 0;
    max-width: 320px;
    color: var(--text-3);
    font-size: var(--font-md);
    line-height: 1.5;
  }

  .empty-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: var(--gap-sm);
    margin-top: 6px;
  }

  @media (hover: hover) and (pointer: fine) {
    .tab:hover {
      color: var(--text-1);
      background: var(--control-hover);
    }

    .share-gutter:hover,
    .col-gutter:hover {
      background: color-mix(in srgb, var(--accent) 30%, transparent);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .tab,
    .share-gutter,
    .col-gutter {
      transition: none;
    }
  }
</style>
