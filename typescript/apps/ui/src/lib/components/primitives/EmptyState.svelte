<script lang="ts">
  import type { Snippet } from "svelte";

  /**
   * What a pane says when it has nothing to show: a title, an optional note, and an
   * optional snippet the reader can paste to change that. Every pane draws its empty
   * state with this, so they read as one thing side by side.
   */
  interface Props {
    title: string;
    /** Code that would make the pane say something, drawn as a code block. */
    code?: string;
    /** The code's language, shown as the block's tag. */
    language?: string;
    children?: Snippet;
  }

  let { title, code, language, children }: Props = $props();
</script>

<div class="empty-state">
  <p class="empty-title">{title}</p>
  {#if children}
    <div class="empty-note">{@render children()}</div>
  {/if}
  {#if code}
    <pre dir="ltr" data-language={language}>{code}</pre>
  {/if}
</div>

<style>
  .empty-state {
    display: grid;
    align-content: start;
    justify-items: start;
    gap: var(--gutter-tight);
    min-width: 0;
    padding: var(--gutter);
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-3);
    font-size: var(--font-sm);
    line-height: 1.5;
  }

  .empty-title {
    margin: 0;
    color: var(--text-1);
    font-weight: 600;
  }

  .empty-note {
    display: grid;
    justify-items: start;
    gap: var(--gutter-tight);
  }

  .empty-note :global(p) {
    margin: 0;
  }

  .empty-note :global(code) {
    color: var(--accent-soft);
    font-family: var(--font-mono);
    overflow-wrap: anywhere;
  }

  /* Wrapped rather than scrolled: a pane is often narrower than one line of the
     snippet, and a scroller there shows only its empty track under the code. */
  pre {
    position: relative;
    justify-self: stretch;
    margin: 0;
    /* Room above the code for the language tag, which app.css draws. */
    padding: 30px var(--gutter-tight) var(--gutter-tight);
    border: 1px solid var(--code-block-border);
    background: var(--code-block-bg);
    color: var(--code-block-text);
    font-family: var(--font-mono);
    font-size: var(--font-code);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  pre[data-language]::before {
    position: absolute;
    top: 7px;
    right: 8px;
  }
</style>
