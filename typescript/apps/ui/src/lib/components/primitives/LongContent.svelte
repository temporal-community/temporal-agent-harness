<script lang="ts" module>
  const remembered: Record<string, boolean> = Object.create(null);

  export function exceedsLongContentThreshold(renderedHeight: number, maxHeight: number): boolean {
    return renderedHeight > maxHeight + 1;
  }

  export function mayCollapse(streaming: boolean, renderedHeight: number, maxHeight: number): boolean {
    return !streaming && exceedsLongContentThreshold(renderedHeight, maxHeight);
  }
</script>

<script lang="ts">
  import { ChevronDown, ChevronUp } from "@lucide/svelte";
  import { tick } from "svelte";
  import type { Snippet } from "svelte";
  import type { Attachment } from "svelte/attachments";

  interface Props {
    id: string;
    text?: string;
    streaming?: boolean;
    label?: string;
    lines?: number | null;
    onResize?: () => void;
    children: Snippet;
  }

  let {
    id,
    text = "",
    streaming = false,
    label = "Long content",
    lines = null,
    onResize,
    children
  }: Props = $props();

  const uid = $props.id();
  const regionId = `${uid}-region`;
  let region = $state<HTMLElement | null>(null);
  let collapsible = $state(false);
  let ready = $state(false);
  let expanded = $state(false);
  const lineCount = $derived(lines ?? (text ? text.replace(/\r\n?/g, "\n").split("\n").length : null));
  const collapsed = $derived(collapsible && !expanded && !streaming);

  function threshold(node: HTMLElement): number {
    const styles = getComputedStyle(node);
    const maxHeight = Number.parseFloat(styles.getPropertyValue("--long-content-max-height"));
    if (Number.isFinite(maxHeight)) return maxHeight;
    const maxLines = Number.parseFloat(styles.getPropertyValue("--long-content-max-lines")) || 40;
    const lineHeight = Number.parseFloat(styles.lineHeight) || Number.parseFloat(styles.fontSize) * 1.5;
    return lineHeight * maxLines;
  }

  function measureContent(value: string, isStreaming: boolean): Attachment<HTMLElement> {
    void value;
    return (node) => {
      region = node;
      expanded = remembered[id] ?? false;
      const read = () => {
        collapsible = mayCollapse(isStreaming, node.scrollHeight, threshold(node));
        if (!collapsible) expanded = false;
      };
      read();
      const frame = requestAnimationFrame(() => {
        ready = true;
      });
      if (typeof ResizeObserver === "undefined") return () => cancelAnimationFrame(frame);
      const observer = new ResizeObserver(read);
      observer.observe(node);
      return () => {
        cancelAnimationFrame(frame);
        observer.disconnect();
      };
    };
  }

  function scrollParent(node: HTMLElement): HTMLElement | null {
    let parent = node.parentElement;
    while (parent && !/auto|scroll/.test(getComputedStyle(parent).overflowY)) parent = parent.parentElement;
    return parent;
  }

  async function toggle(): Promise<void> {
    if (!region) return;
    const scroller = scrollParent(region);
    const before = region.getBoundingClientRect().top;
    expanded = !expanded;
    remembered[id] = expanded;
    await tick();
    onResize?.();
    await tick();
    if (scroller) scroller.scrollTop += region.getBoundingClientRect().top - before;
  }
</script>

<div class={["long-content", { collapsible, collapsed, expanded }]}>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex: expanded overflow regions need keyboard scrolling -->
  <div
    id={regionId}
    class={["long-content-region", ready && "animate-size"]}
    role="region"
    aria-label={label}
    tabindex={expanded ? 0 : undefined}
    {@attach measureContent(text, streaming)}
  >
    {@render children()}
  </div>
  {#if collapsible && !streaming}
    <button type="button" class="long-content-toggle" aria-expanded={expanded} aria-controls={regionId} onclick={toggle}>
      {#if expanded}
        <ChevronUp size={13} aria-hidden="true" />
        Show less
      {:else}
        <ChevronDown size={13} aria-hidden="true" />
        {lineCount && lineCount > 1 ? `Show all · ${lineCount.toLocaleString()} lines` : "Show all"}
      {/if}
    </button>
  {/if}
</div>

<style>
  .long-content {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }

  .long-content-region {
    min-width: 0;
    height: auto;
  }

  .collapsed .long-content-region {
    position: relative;
    height: var(--long-content-max-height);
    overflow: hidden;
    mask-image: linear-gradient(to bottom, #000 calc(100% - 4lh), transparent);
  }

  .expanded .long-content-region {
    overflow: auto;
  }

  .long-content-toggle {
    position: sticky;
    bottom: var(--gap-sm);
    z-index: 1;
    justify-self: center;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    min-height: var(--control-height-xs);
    margin-top: calc(-1 * (var(--control-height-xs) + var(--gap-sm)));
    margin-bottom: var(--gap-sm);
    padding: 0 8px;
    border: 0;
    border-radius: var(--radius-sm);
    background: var(--surface-3);
    box-shadow: var(--shadow-floating);
    color: var(--text-2);
    font: inherit;
    font-size: var(--font-sm);
    cursor: pointer;
  }

  /* Over the fade while collapsed; open, there is no fade, so it takes a line of its own rather
     than covering the content's last line. */
  .expanded .long-content-toggle {
    margin-top: 0;
  }

  @media (hover: hover) and (pointer: fine) {
    .long-content-toggle:hover {
      color: var(--text-1);
      background: var(--surface-4);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .long-content-region {
      transition: none !important;
    }
  }
</style>
