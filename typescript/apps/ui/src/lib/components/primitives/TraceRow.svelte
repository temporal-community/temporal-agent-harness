<script lang="ts">
  import { ChevronRight } from "@lucide/svelte";
  import type { Snippet } from "svelte";
  import StateGlyph from "$lib/components/primitives/StateGlyph.svelte";
  import { NEST_CAP } from "$lib/components/primitives/nest";
  import type { StatusKind } from "$lib/components/primitives/StatusChip.svelte";

  /**
   * One line of a trace, drawn the same in the chat's turn trace and the Logs pane: a state
   * glyph, the words, a muted figure at the end. Hue goes on the glyph and the status word and
   * nowhere else. A row with details is the button that opens them (app.css `.disclosure`),
   * and `children` are those details; its glyph turns into the caret when pointed at or open.
   * A row without is not a button and keeps its glyph.
   * The caller owns the density (padding, height) and anything drawn around the rows.
   */
  interface Props {
    /** The state the glyph draws; it also colours the status word. */
    glyph?: StatusKind | null;
    running?: boolean;
    /** The state in words for screen readers, where the glyph is the only thing saying it. */
    glyphLabel?: string;
    /** Plain words before the name: "host", or a log event's label. */
    prefix?: string;
    name?: string;
    /** The name is an identifier (a tool or a model), so mono. */
    mono?: boolean;
    /** A word that needs attention, in the glyph's hue: "jev checking", "rejected". */
    status?: string;
    /** A token count or a duration: muted like the note, but it never gives way. */
    figure?: string;
    /** Muted context that gives way first: who approved, an error's first line. */
    note?: string;
    /** The trailing figure: a duration, or a clock time. */
    meta?: string;
    tip?: string;
    /** Levels in, for the line only. Past NEST_CAP a row keeps the cap's indent. */
    depth?: number;
    expandable?: boolean;
    expanded?: boolean;
    controls?: string;
    onToggle?: () => void;
    /** A row that follows a reference rather than revealing details in place. */
    onActivate?: () => void;
    /** Drawn under the line whether or not the row is open: a tool's images in the chat trace. */
    media?: Snippet;
    children?: Snippet;
  }

  let {
    glyph = null,
    running = false,
    glyphLabel,
    prefix,
    name,
    mono = false,
    status,
    figure,
    note,
    meta = "",
    tip,
    depth = 0,
    expandable = false,
    expanded = false,
    controls,
    onToggle,
    onActivate,
    media,
    children
  }: Props = $props();
</script>

{#snippet line()}
  <!-- Kept when empty, so every row's words start on one line. -->
  <span class="trace-glyph">
    {#if glyph}<StateGlyph kind={glyph} {running} size={13} />{/if}
    {#if expandable}<ChevronRight class="disclosure-chevron" size={13} aria-hidden="true" />{/if}
  </span>
  <span class="trace-copy">
    {#if prefix}<span class="trace-prefix fade-end" dir="auto">{prefix}</span>{/if}
    {#if name}<strong class={[{ mono }, "fade-end"]} dir="auto">{name}</strong>{/if}
    {#if status}<span class="trace-status">{status}</span>{/if}
    {#if figure}<span class="trace-figure">{figure}</span>{/if}
    {#if note}<span class="trace-note fade-end" dir="auto">{note}</span>{/if}
    {#if glyphLabel}<span class="visually-hidden">{glyphLabel}</span>{/if}
  </span>
  <span class="trace-meta" dir="auto">{meta}</span>
{/snippet}

<div
  class={["trace-row", depth > 0 && "nested", glyph && `glyph-${glyph}`]}
  style:--depth={depth > 0 ? Math.min(depth, NEST_CAP) : undefined}
>
  {#if expandable || onActivate}
    <button
      type="button"
      class={["trace-line disclosure", running && "live"]}
      aria-expanded={expandable ? expanded : undefined}
      aria-controls={expandable ? controls : undefined}
      data-tip={tip}
      onclick={expandable ? onToggle : onActivate}
    >
      {@render line()}
    </button>
  {:else}
    <div class="trace-line" data-tip={tip}>{@render line()}</div>
  {/if}
  {@render media?.()}
  {#if expandable && expanded}{@render children?.()}{/if}
</div>

<style>
  .trace-row {
    --status-color: var(--accent);
    min-width: 0;
    display: grid;
    gap: 6px;
  }

  .glyph-complete { --status-color: var(--text-3); }
  .glyph-approval { --status-color: var(--live); }
  .glyph-error { --status-color: var(--error); }

  /* A call a Code Mode script made, or a subagent's event: its line steps in a level per depth.
     Its details and media do not: they span the row at any depth, so a deep row's payload is not
     squeezed into what the indent leaves. */
  .trace-row.nested > .trace-line {
    width: calc(100% - var(--depth) * 20px);
    margin-inline-start: calc(var(--depth) * 20px);
  }

  .trace-line {
    width: 100%;
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 8px;
    align-items: center;
    padding: 0;
    border: 0;
    border-radius: var(--radius-sm);
    color: var(--text-3);
    background: transparent;
    font: inherit;
    font-size: var(--font-md);
    text-align: start;
  }

  @media (hover: hover) and (pointer: fine) {
    .trace-line.disclosure:hover {
      color: var(--text-2);
    }
  }

  .trace-line:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
    outline-offset: 2px;
  }

  /* One line at every width: the note gives way first, then the name, each fading at its end
     (app.css .fade-end); the status word and the figure never. */
  .trace-copy {
    min-width: 0;
    overflow: hidden;
    display: inline-flex;
    gap: 7px;
    align-items: baseline;
    white-space: nowrap;
  }

  .trace-copy > :is(.trace-prefix, strong, .trace-note) {
    min-width: 0;
  }

  .trace-prefix {
    flex: none;
    max-width: 100%;
    color: var(--text-4);
  }

  strong {
    flex: 0 1 auto;
    color: var(--text-2);
    font-weight: 650;
  }

  .mono {
    font-family: var(--font-mono);
    font-size: var(--figure-size);
    unicode-bidi: plaintext;
  }

  .trace-status {
    flex: none;
    color: var(--status-color);
  }

  /* A number in any language, so isolated left to right. */
  .trace-figure {
    flex: none;
    direction: ltr;
    unicode-bidi: isolate;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }

  .trace-note {
    flex: 1 1 0;
    color: var(--text-3);
  }

  .trace-meta {
    min-width: 38px;
    color: var(--text-3);
    font-size: var(--font-sm);
    font-variant-numeric: tabular-nums;
    text-align: right;
    white-space: nowrap;
  }
</style>
