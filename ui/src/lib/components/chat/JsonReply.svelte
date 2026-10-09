<script lang="ts" module>
  /** Lines a folded payload shows. */
  export const COLLAPSED_LINES = 10;
  /** Lines a payload may run to before it folds — past the cap with slack, so a fold never hides a line or two. */
  export const FOLD_AFTER_LINES = 14;

  /** The payload as drawn and as copied. */
  export const prettyJson = (json: unknown): string => JSON.stringify(json, null, 2);

  /** The first `count` lines of `text`, found without splitting all of it. */
  export function headLines(text: string, count: number): string {
    let end = -1;
    for (let line = 0; line < count; line += 1) {
      end = text.indexOf("\n", end + 1);
      if (end === -1) return text;
    }
    return text.slice(0, end);
  }

  /** Text a small scroller draws at most: laying out a megabyte costs most of a second per change. */
  export const DRAWN_CHARS = 16_000;

  /**
   * The part of a long text a small scroller draws: the tail of a stream, which is what it
   * follows, or the head of a finished text, which is where it opens. Cut at a line break when
   * there is one nearby. The rest is a scrollback limit, not a loss — the whole text is copied
   * and inspected elsewhere.
   */
  export function drawnPart(text: string, tail: boolean): string {
    if (text.length <= DRAWN_CHARS) return text;
    if (tail) {
      const from = text.indexOf("\n", text.length - DRAWN_CHARS);
      return `…${text.slice(from === -1 || from > text.length - DRAWN_CHARS / 2 ? text.length - DRAWN_CHARS : from)}`;
    }
    const to = text.lastIndexOf("\n", DRAWN_CHARS);
    return `${text.slice(0, to < DRAWN_CHARS / 2 ? DRAWN_CHARS : to)}\n…`;
  }

  /** AgentStateNode's rule for tailing streamed text. Two pixels of slack: line boxes land on
   *  fractional heights, so an exact comparison reports "not at the end" while sitting there. */
  export function atEnd(element: { scrollHeight: number; scrollTop: number; clientHeight: number }): boolean {
    return element.scrollHeight - element.scrollTop - element.clientHeight <= 2;
  }
</script>

<script lang="ts">
  import { ChevronDown, ChevronUp } from "@lucide/svelte";
  import { tick } from "svelte";
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import { highlight } from "$lib/highlight";
  import { stripJsonFence } from "$lib/state/handlerReply";

  interface Props {
    /** The handler's structured output. Absent while the reply is still streaming. */
    json?: unknown;
    /** The streamed reply so far, shown unparsed until `json` arrives. */
    text?: string;
  }

  let { json, text = "" }: Props = $props();

  let expanded = $state(false);
  let scroller = $state<HTMLElement | null>(null);
  /* Tail the stream while the reader is at its end; once they scroll up to read, arriving
     text and scrub steps leave them where they are. Recomputed on every scroll, not latched. */
  let following = true;
  /* Whether the stream has scrolled the scroller, so settling has something to undo. */
  let tailed = false;

  const streaming = $derived(json === undefined);
  const body = $derived(streaming ? stripJsonFence(text) : prettyJson(json));
  const empty = $derived(!streaming && (body === "{}" || body === "[]"));
  /* Not counted while streaming: nothing reads it then, and it would be another pass per delta. */
  const lineCount = $derived(streaming ? 0 : body.split("\n").length);
  const collapsible = $derived(!streaming && lineCount > FOLD_AFTER_LINES);
  /* Folded, only the lines on screen are drawn — not the whole payload clipped by CSS. A
     stream draws only its tail, so a delta never lays out everything streamed so far. */
  const shown = $derived(
    streaming ? drawnPart(body, true) : collapsible && !expanded ? headLines(body, COLLAPSED_LINES) : body
  );
  /* The scroller is this component's own element, outside the `{@html}` that is redrawn on
     every new text, so it keeps its place across deltas and scrub steps. Jumped, never animated.
     A settled reply that never streamed here is left alone: writing scrollTop forces a layout
     of the whole page, and a long session mounts hundreds of them. */
  $effect(() => {
    const element = scroller;
    if (!element) return;
    shown;
    if (streaming) {
      if (following) element.scrollTop = element.scrollHeight;
      tailed = true;
    } else if (tailed) {
      element.scrollTop = 0;
      tailed = false;
    }
  });

  function handleScroll(): void {
    if (scroller && streaming) following = atEnd(scroller);
  }

  /* The clipboard refused: unfold, so the selection the reader copies by hand is all of it. */
  async function selectAll(): Promise<HTMLElement | null> {
    expanded = true;
    await tick();
    return scroller;
  }

  /* The message list does not scroll-anchor, so folding from deep inside a long payload would
     leave the reader far below it. Hold the toggle where it was on screen by scrolling only the
     nearest scroller — not scrollIntoView, which also scrolls the pane rail (see followScroll.ts). */
  async function toggle(event: MouseEvent): Promise<void> {
    const button = event.currentTarget as HTMLElement;
    const before = button.getBoundingClientRect().top;
    expanded = !expanded;
    if (expanded) return;
    await tick();
    let list = button.parentElement;
    while (list && !/auto|scroll/.test(getComputedStyle(list).overflowY)) list = list.parentElement;
    if (list) list.scrollTop += button.getBoundingClientRect().top - before;
  }
</script>

<div
  class="json-reply"
  class:streaming
  class:empty
  class:collapsible
  class:collapsed={collapsible && !expanded}
  style:--json-reply-lines={COLLAPSED_LINES}
>
  <!-- Copy first, so the tag holds the right edge and nothing is reserved while streaming. -->
  <div class="json-head">
    {#if empty}
      <span class="json-empty">Empty result</span>
    {/if}
    {#if !streaming}
      <Copyable value={body} label="Copy JSON" select={selectAll} data-tip-align="end" data-tip-below />
    {/if}
    <span class="code-language-tag">JSON</span>
  </div>
  {#if !empty}
    <!-- Always left to right: an RTL page would otherwise flip its punctuation and indent. A
         scroller is a keyboard stop on its own, so it is named but given no tabindex. -->
    <div class="json-body" dir="ltr" role="region" aria-label="JSON reply" bind:this={scroller} onscroll={handleScroll}>
      <!-- A stream is drawn plain: re-highlighting everything so far on every delta is
           O(total) per chunk, which is what froze a megabyte reply for a second a delta. -->
      <pre class="json-code"><code>{#if streaming}{shown}{:else}{@html highlight(shown, "json")}{/if}</code></pre>
    </div>
  {/if}
  {#if collapsible}
    <button type="button" class="json-toggle" aria-expanded={expanded} onclick={toggle}>
      {#if expanded}
        <ChevronUp size={13} aria-hidden="true" />
        Show less
      {:else}
        <ChevronDown size={13} aria-hidden="true" />
        Show all {lineCount.toLocaleString()} lines
      {/if}
    </button>
  {/if}
</div>

<style>
  /* The frame is the code block's, drawn here so the header and the scroller can be ours. */
  .json-reply {
    --json-reply-line: calc(1.55 * var(--font-md));
    --json-reply-toggle-gap: 8px;
    position: relative;
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    border: 1px solid var(--code-block-border);
    border-radius: var(--radius-md);
    background: var(--code-block-bg);
    box-shadow: var(--code-block-shadow);
  }

  /* Nothing to frame: the result reads as a note, with its copy and tag beside it. */
  .json-reply.empty {
    width: fit-content;
    border-color: transparent;
    background: none;
    box-shadow: none;
  }

  .json-head {
    height: 30px;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--gap-xs);
    padding: 0 8px;
  }

  .json-empty {
    margin-inline-end: auto;
    color: var(--text-3);
    font-size: var(--font-md);
  }

  /* Always shown: there is no row to hover here, and the copy is the block's main action. */
  .json-head :global(.copy) {
    opacity: 1;
  }

  /* Below the header, so its scrollbars never run alongside the tag. */
  .json-body {
    min-width: 0;
    overflow: auto;
    text-align: start;
  }

  .json-code {
    margin: 0;
    padding: 0 12px 12px;
    color: var(--code-block-text);
    font-family: var(--font-mono);
    font-size: var(--font-md);
    line-height: 1.55;
    tab-size: 2;
  }

  /* The payload keeps its shape: an unbroken token scrolls inside the block rather than
     wrapping a record across lines or pushing the bubble wider. */
  .json-code code {
    white-space: pre;
    overflow-wrap: normal;
  }

  /* A minified reply mid-stream is one long line, so it wraps — but within a capped block
     that scrolls, never as a paragraph the height of the pane. */
  .json-reply.streaming .json-body {
    max-height: calc(var(--json-reply-lines) * var(--json-reply-line) + 12px);
  }

  .json-reply.streaming .json-code code {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  /* Room under the last line for the toggle that sits over it. */
  .json-reply.collapsible .json-code {
    padding-bottom: calc(var(--control-height-xs) + 2 * var(--json-reply-toggle-gap));
  }

  .json-reply.collapsed .json-body {
    max-height: calc(var(--json-reply-lines) * var(--json-reply-line));
    overflow-y: hidden;
    mask-image: linear-gradient(to bottom, #000 calc(100% - 3 * var(--json-reply-line)), transparent);
  }

  /* On the fade when folded. Unfolded it sticks to the bottom of the message list, so a
     245-line payload folds again from wherever the reader is in it. */
  .json-toggle {
    position: sticky;
    bottom: var(--json-reply-toggle-gap);
    justify-self: center;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    height: var(--control-height-xs);
    margin-top: calc(-1 * (var(--control-height-xs) + var(--json-reply-toggle-gap)));
    margin-bottom: var(--json-reply-toggle-gap);
    padding: 0 8px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-md);
    background: var(--surface-3);
    box-shadow: var(--shadow-floating);
    color: var(--text-1);
    font: inherit;
    font-size: var(--font-sm);
    cursor: pointer;
  }

  @media (hover: hover) and (pointer: fine) {
    .json-toggle:hover {
      border-color: var(--accent);
    }
  }
</style>
