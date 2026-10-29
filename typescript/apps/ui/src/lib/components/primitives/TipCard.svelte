<script lang="ts" module>
  import type { Component, Snippet } from "svelte";

  /** One fact in a rich tip. One with no value is left out, never drawn as a placeholder. */
  export interface TipField {
    key: string;
    value: string | null | undefined;
    /** An identifier, hash or path: monospace, broken anywhere, and kept at both ends. */
    mono?: boolean;
    /** Drawn instead of the key, which is then not shown or read: the value must say what it is. */
    icon?: Snippet;
    /** A Lucide icon drawn the way `icon` is, for fields built outside a component. */
    glyph?: Component<{ size?: number | string }>;
  }

  /** Past this many characters (two lines of a full-width tip) an identifier keeps its two
   *  ends, which is where ids differ. A 36-character UUID with a prefix still shows whole. */
  export const MONO_MAX = 72;

  const graphemes = new Intl.Segmenter(undefined, { granularity: "grapheme" });

  export function middle(value: string, max = MONO_MAX): string {
    const parts = Array.from(graphemes.segment(value), (part) => part.segment);
    if (parts.length <= max) return value;
    const head = Math.ceil((max - 1) / 2);
    return `${parts.slice(0, head).join("")}…${parts.slice(parts.length - (max - 1 - head)).join("")}`;
  }

  export function shownFields(fields: readonly TipField[]): TipField[] {
    return fields.filter((field) => field.value != null && field.value.trim() !== "");
  }
</script>

<script lang="ts">
  import type { HTMLAttributes } from "svelte/elements";

  /**
   * More than one fact about something, laid out to be read at a glance: an optional heading,
   * then muted keys beside their values, then free text. Inside a `data-tip` element it is that
   * element's tip (app.css places and shows it); anywhere else it is a card of its own.
   * A span throughout, so a tip can sit inside the button it describes.
   */
  interface Props extends HTMLAttributes<HTMLSpanElement> {
    /** Only what the thing being described does not already show. */
    heading?: string | null;
    fields?: readonly TipField[];
    /** Prose under the fields: an event's message, say. */
    body?: string | null;
  }

  let { heading = null, fields = [], body = null, class: className, ...rest }: Props = $props();

  const shown = $derived(shownFields(fields));
</script>

<span {...rest} class={["tip-card", className]}>
  {#if heading?.trim()}<span class="tip-card-heading" dir="auto">{heading}</span>{/if}
  {#each shown as field (field.key)}
    {#if field.icon}
      <span class="tip-card-icon" aria-hidden="true">{@render field.icon()}</span>
    {:else if field.glyph}
      <span class="tip-card-icon" aria-hidden="true"><field.glyph size={12} /></span>
    {:else}
      <span class="tip-card-key">{field.key}</span>
    {/if}
    <span class={["tip-card-value", field.mono && "mono"]} dir="auto">{field.mono ? middle(field.value!) : field.value}</span>
  {/each}
  {#if body?.trim()}<span class="tip-card-body" dir="auto">{body}</span>{/if}
</span>

<style>
  /* `display: grid` is app.css's, so a tip's card stays hidden until its element shows it. */
  .tip-card {
    grid-template-columns: auto minmax(0, 1fr);
    align-items: baseline;
    gap: var(--gap-2xs) var(--gap-md);
    text-align: start;
    white-space: normal;
  }

  .tip-card-heading,
  .tip-card-body {
    grid-column: 1 / -1;
  }

  .tip-card-heading,
  .tip-card-value,
  .tip-card-body {
    min-width: 0;
    overflow-wrap: anywhere;
    unicode-bidi: plaintext;
  }

  .tip-card-heading {
    margin-bottom: var(--gap-2xs);
    color: var(--text-1);
    font-weight: 500;
  }

  /* The label register, a step brighter than a pane's: --text-3 is under 4.5:1 on --surface-3. */
  .tip-card-key {
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--label-size);
    font-weight: var(--label-weight);
    letter-spacing: var(--label-tracking);
    text-transform: uppercase;
    white-space: nowrap;
  }

  /* Centred on its value's first line, and dim the way a key is; still 3:1 on --surface-3. */
  .tip-card-icon {
    align-self: start;
    height: 1lh;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-3);
  }

  .tip-card-icon > :global(*) {
    flex-shrink: 0;
  }

  .tip-card-value {
    color: var(--text-1);
    font-weight: 400;
    font-variant-numeric: tabular-nums;
  }

  /* Every character is a break, not just the hyphens, so an id fills its lines evenly. */
  .tip-card-value.mono {
    font-family: var(--font-mono);
    word-break: break-all;
  }

  .tip-card-body {
    margin-top: var(--gap-2xs);
    color: var(--text-2);
    font-weight: 400;
    white-space: pre-line;
  }

  /* A tip cannot be scrolled or selected, so it stops growing, at two lines a fact; what it
     cuts, the thing it describes holds whole (the session, the event). Identifiers are cut in
     the middle instead. A card whose heading is its point sets --tip-card-heading-lines. */
  :global([data-tip]) > .tip-card :is(.tip-card-heading, .tip-card-value:not(.mono)) {
    display: -webkit-box;
    overflow: hidden;
    -webkit-box-orient: vertical;
  }

  :global([data-tip]) > .tip-card .tip-card-heading {
    -webkit-line-clamp: var(--tip-card-heading-lines, 2);
    line-clamp: var(--tip-card-heading-lines, 2);
  }

  :global([data-tip]) > .tip-card .tip-card-value:not(.mono) {
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  /* An 800px window at 200% zoom: a Session Manager row has ~125px either side of it. */
  @media (max-height: 520px) {
    :global([data-tip]) > .tip-card :is(.tip-card-heading, .tip-card-value:not(.mono)) {
      -webkit-line-clamp: 1;
      line-clamp: 1;
    }
  }

  :global([data-tip]) > .tip-card .tip-card-body {
    display: -webkit-box;
    overflow: hidden;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 4;
    line-clamp: 4;
  }
</style>
