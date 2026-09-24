<script lang="ts">
  import type { Snippet } from "svelte";

  type DrawerEdge = "bottom" | "left";

  interface Props {
    edge: DrawerEdge;
    label: string;
    size: number;
    minSize: number;
    onResize: (size: number) => void;
    onResizeStart?: () => void;
    onFit: () => void;
    children: Snippet;
  }

  let {
    edge,
    label,
    size,
    minSize,
    onResize,
    onResizeStart,
    onFit,
    children
  }: Props = $props();

  let element = $state<HTMLElement | null>(null);
  let resizing = $state(false);

  function sizeFrom(event: PointerEvent): number {
    const rect = element?.getBoundingClientRect();
    if (!rect) return size;
    switch (edge) {
      case "bottom":
        return Math.round(rect.bottom - event.clientY);
      case "left":
        return Math.round(event.clientX - rect.left);
      default: {
        const _exhaustive: never = edge;
        throw new Error(`unhandled drawer edge: ${_exhaustive}`);
      }
    }
  }

  function resizeFrom(event: PointerEvent): void {
    const next = sizeFrom(event);
    onResize(next < minSize ? 0 : next);
  }

  function startResize(event: PointerEvent): void {
    if (event.button !== 0 && event.pointerType !== "touch") return;
    event.preventDefault();
    resizing = true;
    onResizeStart?.();
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    resizeFrom(event);
  }

  function moveResize(event: PointerEvent): void {
    if (resizing) resizeFrom(event);
  }

  function stopResize(event: PointerEvent): void {
    resizing = false;
    const handle = event.currentTarget as HTMLElement;
    if (handle.hasPointerCapture(event.pointerId)) {
      handle.releasePointerCapture(event.pointerId);
    }
  }

  const resizeLabel = $derived(`Resize the ${edge} drawer`);
  const resizeTitle = $derived(
    edge === "bottom"
      ? "Drag to set the drawer height — double-click to fit it to the trace"
      : "Drag to set the drawer width — double-click to reset it"
  );
</script>

<section
  class:bottom={edge === "bottom"}
  class:left={edge === "left"}
  class:shut={size === 0}
  class:resizing
  class="docked-drawer"
  bind:this={element}
  aria-label={label}
>
  <button
    type="button"
    class="drawer-gutter"
    aria-label={resizeLabel}
    title={resizeTitle}
    onpointerdown={startResize}
    onpointermove={moveResize}
    onpointerup={stopResize}
    onpointercancel={stopResize}
    ondblclick={onFit}
  ></button>

  <div class="drawer-body">
    {@render children()}
  </div>
</section>

<style>
  .docked-drawer {
    position: relative;
    width: 100%;
    height: 100%;
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template: minmax(0, 1fr) / minmax(0, 1fr);
    background: var(--surface-1);
  }

  .docked-drawer.bottom {
    border-top: 1px solid var(--border);
  }

  .docked-drawer.left {
    border-right: 1px solid var(--border);
  }

  .docked-drawer.resizing,
  .docked-drawer.resizing * {
    user-select: none;
  }

  /* A grid of one cell, not a flex row: the content is height- and width-agnostic and
     takes whatever box its parent gives it, and a flex child with no `flex` is given
     its content width — a bottom drawer holding one pane would shrink to that column's
     width instead of the drawer's. */
  .drawer-body {
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template: minmax(0, 1fr) / minmax(0, 1fr);
    overflow: hidden;
  }

  /* One handle mirrored across two edges. It straddles the seam it moves and is
     otherwise invisible, exactly like pane and split gutters. */
  .drawer-gutter {
    position: absolute;
    z-index: 5;
    padding: 0;
    border: 0;
    background: transparent;
    touch-action: none;
    transition: background var(--duration-fast) var(--ease-out);
  }

  .bottom .drawer-gutter {
    inset: -6px 0 auto 0;
    height: 12px;
    cursor: row-resize;
  }

  .left .drawer-gutter {
    inset: 0 -6px 0 auto;
    width: 12px;
    cursor: col-resize;
  }

  .bottom.shut .drawer-gutter {
    inset: -11px 0 auto 0;
  }

  .left.shut .drawer-gutter {
    inset: 0 -11px 0 auto;
  }

  .drawer-gutter:focus-visible {
    background: color-mix(in srgb, var(--accent) 30%, transparent);
    outline: 2px solid var(--focus-ring);
    outline-offset: -4px;
  }

  @media (hover: hover) and (pointer: fine) {
    .drawer-gutter:hover {
      background: color-mix(in srgb, var(--accent) 30%, transparent);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .drawer-gutter {
      transition: none;
    }
  }
</style>
