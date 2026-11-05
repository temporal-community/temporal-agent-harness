<script lang="ts">
  import type { Component, Snippet } from "svelte";

  /**
   * What a pane says when it has nothing to show: a title, an optional note, and an
   * optional snippet the reader can paste to change that. Every pane draws its empty
   * state with this, so they read as one thing side by side.
   */
  interface Props {
    title: string;
    /** A muted glyph above the title naming what would be here, e.g. braces for state. */
    icon?: Component;
    /** Code that would make the pane say something, drawn as a code block. */
    code?: string;
    /** The code's language, shown as the block's tag. */
    language?: string;
    children?: Snippet;
  }

  let { title, icon: Icon, code, language, children }: Props = $props();
</script>

<div class="empty-state">
  {#if Icon}
    <span class="empty-icon" aria-hidden="true"><Icon size={18} strokeWidth={1.75} /></span>
  {/if}
  <p class="empty-title">{title}</p>
  {#if children}
    <div class="empty-note">{@render children()}</div>
  {/if}
  {#if code}
    <pre dir="ltr" data-language={language}>{code}</pre>
  {/if}
</div>

<style>
  /* Text on the pane, with no box: in this console a box means "press me", and there
     is nothing here to press. Sized like the no-session welcome so the two read as one. */
  .empty-state {
    display: grid;
    align-content: start;
    justify-items: start;
    gap: 6px;
    min-width: 0;
    max-width: 560px;
    padding: var(--gutter);
    color: var(--text-3);
    font-size: var(--font-md);
    line-height: 1.5;
  }

  /* Bare: a filled tile around it would read as an icon button. */
  .empty-icon {
    display: inline-flex;
    margin-bottom: 4px;
    color: var(--text-4);
  }

  .empty-title {
    margin: 0;
    color: var(--text-1);
    font-size: var(--font-xl);
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

  /* A hint under the note, not the pane's content: a neutral fill with no frame, so the
     title leads. Wrapped rather than scrolled: a pane is often narrower than one line of
     the snippet, and a scroller there shows only its empty track under the code. */
  pre {
    position: relative;
    justify-self: stretch;
    margin: 10px 0 0;
    /* Room above the code for the language tag, which app.css draws. */
    padding: 26px 12px 10px;
    background: var(--surface-2);
    color: var(--code-block-text);
    font-family: var(--font-mono);
    font-size: var(--font-code);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  /* A plain label: app.css draws the tag as a filled chip, the loudest thing left here. */
  pre[data-language]::before {
    position: absolute;
    top: 6px;
    right: 8px;
    padding: 0;
    border: 0;
    background: none;
    color: var(--text-4);
  }
</style>
