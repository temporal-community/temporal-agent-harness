<script lang="ts">
  import type { Snippet } from "svelte";
  import { ChevronsLeft, Pin, Rows2, SquareStack, X } from "@lucide/svelte";
  import { PANE_META, titleBelongsInHead } from "$lib/panes/registry";
  import type { Pane } from "$lib/state/paneStack.svelte";

  interface Props {
    pane: Pane;
    /** Sharing its column, so the column folds and resizes as a whole. */
    shared?: boolean;
    /** Shared by being stacked with everything visible, rather than as tabs. */
    split?: boolean;
    title: string;
    /** CSS custom property for the status dot, omitted when there is no status. */
    statusTone?: string | null;
    statusLabel?: string | null;
    focused: boolean;
    canClose: boolean;
    canResize: boolean;
    onFocus: () => void;
    onToggleCollapse: () => void;
    onTogglePin: () => void;
    onClose: () => void;
    onResize: (size: number | null) => void;
    /** Flip the column between stacked and tabbed. Only shown on a shared one. */
    onToggleSplit: () => void;
    onDragStart: (event: DragEvent) => void;
    onDragEnd: () => void;
    content: Snippet;
  }

  let {
    pane,
    shared = false,
    split = false,
    title,
    statusTone = null,
    statusLabel = null,
    focused,
    canClose,
    canResize,
    onFocus,
    onToggleCollapse,
    onTogglePin,
    onClose,
    onResize,
    onToggleSplit,
    onDragStart,
    onDragEnd,
    content
  }: Props = $props();

  const RESIZE_STEP = 24;

  let element = $state<HTMLElement | null>(null);
  let resizing = $state(false);

  const meta = $derived(PANE_META[pane.kind]);

  /* Exactly one heading per pane, wherever the pane's name is stated: in the
     body for a pane that names its subject there, otherwise in the head. Losing
     the head's `h2` outright would take those panes out of a screen reader's
     heading list, and the badge is their name. */
  const headNamesPane = $derived(!meta.headline);
  /* A pane whose subject has not resolved yet is titled after its own kind —
     "Subagent" before the run names the agent — and the badge has said that. */
  const showTitle = $derived(
    titleBelongsInHead(pane.id) &&
      title.trim().toLowerCase() !== meta.kindLabel.toLowerCase()
  );

  function currentSize(): number {
    return pane.size ?? meta.defaultSize;
  }

  function resizeFromPointer(event: PointerEvent): void {
    const rect = element?.getBoundingClientRect();
    if (!rect) return;
    onResize(Math.max(meta.minSize, Math.round(event.clientX - rect.left)));
  }

  function startResize(event: PointerEvent): void {
    if (event.button !== 0 && event.pointerType !== "touch") return;
    event.preventDefault();
    resizing = true;
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    resizeFromPointer(event);
  }

  function moveResize(event: PointerEvent): void {
    if (resizing) resizeFromPointer(event);
  }

  function stopResize(event: PointerEvent): void {
    resizing = false;
    const handle = event.currentTarget as HTMLElement;
    if (handle.hasPointerCapture(event.pointerId)) {
      handle.releasePointerCapture(event.pointerId);
    }
  }

  function handleResizeKeydown(event: KeyboardEvent): void {
    /* Modified arrows belong to the rail: they walk and reorder panes. */
    if (event.altKey || event.metaKey || event.ctrlKey || event.shiftKey) return;
    if (event.key === "Home") {
      event.preventDefault();
      onResize(null);
      return;
    }
    let next = currentSize();
    if (event.key === "ArrowLeft") next -= RESIZE_STEP;
    else if (event.key === "ArrowRight") next += RESIZE_STEP;
    else return;
    event.preventDefault();
    onResize(Math.max(meta.minSize, next));
  }
</script>

{#if pane.collapsed}
  <button
    type="button"
    class="spine"
    title={statusLabel ? `Expand ${title} — ${statusLabel}` : `Expand ${title}`}
    aria-label={statusLabel ? `Expand ${title}, ${statusLabel}` : `Expand ${title}`}
    ondragover={(event) => event.preventDefault()}
    onclick={onToggleCollapse}
  >
    <!-- A pip with no status to carry would be decoration, so it only appears
         when the pane has state to report. -->
    {#if statusLabel}
      <span
        class="spine-dot"
        style={statusTone ? `background: var(${statusTone})` : undefined}
      ></span>
    {/if}
    <span class="spine-title">{title}</span>
    <span class="spine-kind" style={`color: var(${statusTone ?? meta.accent})`}>{meta.kindLabel}</span>
  </button>
{:else}
  <section
    class="pane"
    class:focused
    class:resizing
    bind:this={element}
    tabindex="-1"
    aria-label={`${meta.kindLabel}: ${title}`}
    onfocusin={onFocus}
    onpointerdown={onFocus}
    ondragover={(event) => event.preventDefault()}
  >
    <!-- The header is the pane's drag handle; the buttons inside it remain the
         only focusable controls. -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <header
      class="pane-head"
      role="group"
      aria-label={`${meta.kindLabel} controls`}
      aria-keyshortcuts="Meta+Shift+ArrowLeft Meta+Shift+ArrowRight Meta+Shift+ArrowUp Meta+Shift+ArrowDown"
      title="Drag to a column's side to move it, its top or bottom to split, its middle to tab — or Cmd+Shift+Arrows"
      draggable="true"
      ondragstart={onDragStart}
      ondragend={onDragEnd}
    >
      <!-- One row: the badge names the kind, and the title follows it only when
           it names something the badge does not. -->
      <svelte:element this={headNamesPane ? "h2" : "div"} class="head-name">
        <span class="kicker pane-kind" style={`--pane-accent: var(${statusTone ?? meta.accent})`}>
          {meta.kindLabel}
        </span>
        {#if showTitle}
          <span class="head-title" title={title}>{title}</span>
        {/if}
      </svelte:element>
      <!-- Only on a shared column, which is the only place it means anything —
           and it appears exactly when a reader has just made one, which is when
           they want to know the other arrangement exists. -->
      {#if shared}
        <button
          type="button"
          class="head-button"
          data-arrange={split ? "split" : "tabs"}
          title={split ? "Show this column as tabs" : "Split the column, both visible"}
          aria-label={split
            ? `Show the column holding ${title} as tabs`
            : `Split the column holding ${title} so every pane stays visible`}
          onclick={onToggleSplit}
        >
          {#if split}
            <SquareStack size={13} />
          {:else}
            <Rows2 size={13} />
          {/if}
        </button>
      {/if}
      <button
        type="button"
        class="head-button"
        class:on={pane.pinned}
        aria-pressed={pane.pinned}
        title={pane.pinned ? "Unpin pane" : "Pin pane open"}
        onclick={onTogglePin}
      >
        <Pin size={12} />
      </button>
      <button
        type="button"
        class="head-button"
        title={shared ? "Collapse the column to a spine" : "Collapse to a spine"}
        aria-label={shared
          ? `Collapse the column holding ${title} to a spine`
          : `Collapse ${title} to a spine`}
        onclick={onToggleCollapse}
      >
        <ChevronsLeft size={13} />
      </button>
      {#if canClose}
        <button
          type="button"
          class="head-button danger"
          title="Close pane"
          aria-label={`Close ${title}`}
          onclick={onClose}
        >
          <X size={13} />
        </button>
      {/if}
    </header>

    <div class="pane-body">
      {@render content()}
    </div>

    {#if canResize}
      <button
        type="button"
        class="resize-gutter"
        aria-label={shared ? `Resize the column holding ${title}` : `Resize ${title}`}
        aria-keyshortcuts="ArrowLeft ArrowRight Home"
        title={meta.flexible
          ? "Drag to set a width — double-click to let it fill the rail again"
          : "Drag to resize — double-click to reset"}
        onpointerdown={startResize}
        ondblclick={() => onResize(null)}
        onpointermove={moveResize}
        onpointerup={stopResize}
        onpointercancel={stopResize}
        onkeydown={handleResizeKeydown}
      ></button>
    {/if}
  </section>
{/if}

<style>
  /* The column owns its width and the rail owns its height; the shell fills what
     it is given and scrolls its own content inside that. */
  .pane {
    position: relative;
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    box-sizing: border-box;
    border-right: 1px solid var(--border);
    background: var(--surface-1);
    opacity: 1;
    transform: none;
    transition:
      opacity var(--duration-pane) var(--ease-pane),
      transform var(--duration-pane) var(--ease-pane);
  }

  .pane.resizing,
  .pane.resizing * {
    user-select: none;
  }

  /* Panes arrive from the right, matching where they are inserted. A transition
     with @starting-style stays interruptible when panes open in quick
     succession; a keyframe would restart from zero. */
  @starting-style {
    .pane {
      opacity: 0;
      transform: translateX(14px);
    }
  }

  /* The focused pane reads through its head fill and a brighter hairline — no
     accent edge stripe, so the rail stays quiet when several panes are open. */
  .pane.focused .pane-head {
    background: var(--surface-2);
    border-bottom-color: var(--border-strong);
  }

  /* Landing here by keyboard needs a ring the head tint alone does not give. */
  .pane:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  /* One row, so the header is the row: every pane's chrome is the same height
     whether or not it has a title to show, which is what lets a split column
     read as two panes rather than two differently-framed ones. */
  .pane-head {
    flex: none;
    display: flex;
    align-items: center;
    gap: var(--gap-xs);
    padding: var(--gap-md) var(--gutter);
    border-bottom: 1px solid var(--border);
    background: var(--surface-head);
    cursor: grab;
    transition: background var(--duration-fast) var(--ease-out);
  }

  .pane-head:active {
    cursor: grabbing;
  }

  /* Takes the slack, so the controls hold the right edge with or without a
     title beside the badge. */
  .head-name {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    margin: 0;
    font: inherit;
  }

  .pane-kind {
    flex: none;
    padding: 0 var(--gap-xs);
    margin-left: -4px;
    color: var(--pane-accent);
    white-space: nowrap;
  }

  /* Inverting the kind label is how a TUI marks its active window — a filled
     block, not an edge stripe. */
  .pane.focused .pane-kind {
    background: var(--pane-accent);
    color: var(--surface-0);
  }

  .head-button {
    display: grid;
    place-items: center;
    width: var(--icon-target);
    height: var(--icon-target);
    padding: 0;
    border: 1px solid transparent;
    border-radius: var(--radius-xs);
    background: transparent;
    color: var(--text-3);
    cursor: pointer;
    transition:
      color var(--duration-fast) var(--ease-out),
      border-color var(--duration-fast) var(--ease-out),
      transform var(--duration-press) var(--ease-out);
  }

  .head-button:active {
    transform: scale(0.94);
  }

  .head-button.on {
    border-color: var(--border-strong);
    color: var(--text-1);
  }

  .head-button:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 1px;
  }

  /* The title yields before the badge does: a clipped kind badge costs the
     reader the one word every pane of this kind shares, while a clipped
     identifier still starts with the characters that tell it from its siblings.
     The full text stays in the `title` attribute either way. */
  .head-title {
    min-width: 0;
    color: var(--text-1);
    font-size: var(--font-lg);
    font-weight: 450;
    letter-spacing: -0.01em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .pane-body {
    flex: 1 1 0;
    min-height: 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    /* Panes size their own type: a 380px pane and an 800px pane are different
       rooms, and the viewport cannot tell them apart. */
    container: pane / inline-size;
  }

  .resize-gutter {
    position: absolute;
    top: 0;
    right: -6px;
    width: 12px;
    height: 100%;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: col-resize;
    touch-action: none;
    z-index: 3;
  }

  .resize-gutter:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -4px;
  }

  .spine {
    flex: 1;
    width: 100%;
    max-width: 100%;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--gap-md);
    padding: var(--gap-lg) 0;
    box-sizing: border-box;
    border: 0;
    border-right: 1px solid var(--border);
    background: var(--surface-spine);
    cursor: pointer;
    overflow: hidden;
    transition: background var(--duration-fast) var(--ease-out);
  }

  .spine:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  .spine-dot {
    flex: none;
    width: var(--pip);
    height: var(--pip);
    background: var(--text-3);
  }

  .spine-title {
    flex: 1;
    min-height: 0;
    max-width: 100%;
    writing-mode: vertical-rl;
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    color: var(--text-2);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* The one label allowed off the register, and only on size: a 42px spine
     cannot hold 10px at 0.13em turned sideways. Weight and tracking still
     follow, so it reads as the same voice, quieter. Clip rather than widen. */
  .spine-kind {
    flex: none;
    max-width: 100%;
    writing-mode: vertical-rl;
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    font-weight: var(--label-weight);
    letter-spacing: var(--label-tracking);
    text-transform: uppercase;
    overflow: hidden;
  }

  @media (hover: hover) and (pointer: fine) {
    .head-button:hover {
      color: var(--text-1);
      border-color: var(--border);
    }

    .head-button.danger:hover {
      color: var(--error);
    }

    .spine:hover {
      background: var(--control-hover);
    }

    .resize-gutter:hover {
      background: color-mix(in srgb, var(--accent) 30%, transparent);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .pane,
    .pane-head,
    .head-button,
    .spine {
      transition: none;
    }

    .head-button:active {
      transform: none;
    }

    @starting-style {
      .pane {
        opacity: 0;
        transform: none;
      }
    }
  }
</style>
