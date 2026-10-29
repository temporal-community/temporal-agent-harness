<script lang="ts" generics="T">
  import type { Snippet } from "svelte";
  import type { Attachment } from "svelte/attachments";
  import { untrack } from "svelte";
  import { createVirtualizer } from "@tanstack/svelte-virtual";

  /**
   * The rows of one item of an outer windowed list, windowed themselves against the same
   * scroller: a turn holding 100,000 log rows drew every one of them while it was in view.
   *
   * `start` is where the outer item begins in the scroller's content (its virtual item's
   * `start`); this box's own offset inside that item is added to it. A list that is the
   * scroller's own content passes 0, and the scroller is positioned so the offset is its own.
   *
   * `gap` is the space between rows, in pixels: a grid's `gap` has no say over absolute rows.
   */
  let {
    items,
    getKey,
    scroller,
    start,
    estimateSize = 41,
    gap = 0,
    children
  }: {
    items: readonly T[];
    getKey: (item: T, index: number) => string | number;
    scroller: HTMLElement | null;
    start: number;
    estimateSize?: number;
    gap?: number;
    children: Snippet<[T, number]>;
  } = $props();

  /* The box's offset inside the outer item, read from layout once it is drawn and again
     whenever the item resizes: a summary above it that wraps moves it down. */
  let inset = $state(0);
  const readInset: Attachment<HTMLElement> = (node) => {
    const read = () => (inset = node.offsetTop);
    read();
    const parent = node.parentElement;
    if (!parent || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(parent);
    return () => observer.disconnect();
  };

  const rows = createVirtualizer<HTMLElement, HTMLElement>({
    /* Seeded here as well as in the effect below, which SSR never runs. */
    count: untrack(() => items.length),
    getItemKey: (index) => {
      const item = items[index];
      return item === undefined ? index : getKey(item, index);
    },
    estimateSize: () => estimateSize,
    /* Unrounded: the default rounds each row to a whole pixel, and a column of 18.6px code
       lines drawn at 19px drifts a line lower every 50 lines. */
    measureElement: (node, entry) => entry?.borderBoxSize[0]?.blockSize ?? node.getBoundingClientRect().height,
    gap: untrack(() => gap),
    getScrollElement: () => scroller,
    initialRect: { width: 0, height: 800 },
    /* The scroller is shared and already scrolled: a list mounting as its item comes into view
       writes this offset back to it, and the default of 0 threw the reader back to the top. */
    initialOffset: () => scroller?.scrollTop ?? 0,
    scrollMargin: untrack(() => start),
    /* Not `data-index`: that names the outer list's items, which windowedFocus reads. */
    indexAttribute: "data-row-index",
    overscan: 8
  });
  const rowList = () => untrack(() => $rows);
  const measureRow: Attachment<HTMLElement> = (node) => rowList().measureElement(node);

  /* Pre, so a shrunk list's count lands before the rows render and index past its end. */
  $effect.pre(() => {
    rowList().setOptions({ count: items.length, scrollMargin: start + inset });
  });

  /** Scroll the shared scroller so this list's row `index` is in view. */
  export function scrollToIndex(index: number, align: "auto" | "start" = "auto"): void {
    rowList().scrollToIndex(index, { align });
  }

  /** Re-read rows after an in-place disclosure changes height. */
  export function measure(): void {
    rowList().measure();
  }
</script>

<div class="windowed-rows" {@attach readInset} style:height={`${$rows.getTotalSize()}px`}>
  {#each $rows.getVirtualItems() as item (item.key)}
    {@const value = items[item.index]}
    {#if value !== undefined}
      <div
        class="windowed-row"
        data-row-index={item.index}
        style:top={`${item.start - $rows.options.scrollMargin}px`}
        {@attach measureRow}
      >
        {@render children(value, item.index)}
      </div>
    {/if}
  {/each}
</div>

<style>
  .windowed-rows {
    position: relative;
  }

  .windowed-row {
    position: absolute;
    inset-inline: 0;
  }
</style>
