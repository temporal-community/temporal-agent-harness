<script lang="ts" module>
  /** The payload as drawn and as copied. */
  export const prettyJson = (json: unknown): string => JSON.stringify(json, null, 2);

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
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import LongContent from "$lib/components/primitives/LongContent.svelte";
  import { highlight } from "$lib/highlight";
  import { stripJsonFence } from "$lib/state/handlerReply";

  interface Props {
    /** The handler's structured output. Absent while the reply is still streaming. */
    json?: unknown;
    /** The streamed reply so far, shown unparsed until `json` arrives. */
    text?: string;
    id?: string;
    onResize?: () => void;
  }

  let { json, text = "", id = "json-reply", onResize }: Props = $props();

  let scroller = $state<HTMLElement | null>(null);
  /* Tail the stream while the reader is at its end; once they scroll up to read, arriving
     text and scrub steps leave them where they are. Recomputed on every scroll, not latched. */
  let following = true;
  /* Whether the stream has scrolled the scroller, so settling has something to undo. */
  let tailed = false;

  const streaming = $derived(json === undefined);
  const body = $derived(streaming ? stripJsonFence(text) : prettyJson(json));
  const empty = $derived(!streaming && (body === "{}" || body === "[]"));
  const lineCount = $derived(streaming ? 0 : body.split("\n").length);
  const shown = $derived(streaming ? drawnPart(body, true) : body);
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

  const selectAll = (): HTMLElement | null => scroller;
</script>

<div class={["json-reply", { streaming, empty }]}>
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
    <LongContent {id} text={body} {streaming} lines={lineCount} label="JSON reply" {onResize}>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex: a streaming JSON scroller must be keyboard reachable -->
      <div class="json-body" dir="ltr" tabindex={streaming ? 0 : undefined} bind:this={scroller} onscroll={handleScroll}>
        <!-- A stream is drawn plain: re-highlighting everything so far on every delta is
             O(total) per chunk, which is what froze a megabyte reply for a second a delta. -->
        <pre class="json-code"><code>{#if streaming}{shown}{:else}{@html highlight(shown, "json")}{/if}</code></pre>
      </div>
    </LongContent>
  {/if}
</div>

<style>
  /* The frame is the code block's, drawn here so the header and the scroller can be ours. */
  .json-reply {
    --json-reply-line: calc(1.55 * var(--font-md));
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
    height: auto;
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
    max-height: var(--long-content-max-height);
  }

  .json-reply.streaming .json-code code {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

</style>
