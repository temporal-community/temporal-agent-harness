<script lang="ts">
  import type { Snippet } from "svelte";

  /**
   * The one chip in the app. Every tag, status pill, filter, and small chrome
   * button renders through this so they cannot drift apart: two heights — xs for
   * tags inline in content, sm for anything in the app chrome — three fills, one
   * type recipe.
   *
   * Renders a link when given `href`, a button when given `onclick`, and a plain
   * span otherwise, so callers do not have to reimplement the shape to make it
   * interactive.
   */
  export type ChipTone =
    | "neutral"
    | "accent"
    | "model"
    | "reasoning"
    | "tool"
    | "queue"
    | "retry"
    | "success"
    | "error"
    | "live";

  export type ChipSize = "xs" | "sm";

  /** filled: toned wash. quiet: hairline only. bare: no chrome at all. */
  export type ChipFill = "filled" | "quiet" | "bare";

  interface Props {
    label?: string;
    tone?: ChipTone;
    size?: ChipSize;
    fill?: ChipFill;
    /** Selected state for filters and segmented rows. */
    active?: boolean;
    /**
     * Keeps the tone hue on a `quiet` or `bare` chip, which otherwise fall back
     * to --text-3. For controls that have to carry a semantic colour while
     * staying lighter than a filled chip; the hue is used as-is rather than
     * tinted, because those callers are matching an established action colour.
     */
    toned?: boolean;
    /** Draws the breathing outline the live status chip uses. */
    ring?: boolean;
    /** Leading square pip, for chips that stand in for a status. */
    pip?: boolean;
    dense?: boolean;
    disabled?: boolean;
    href?: string | null;
    onclick?: (event: MouseEvent) => void;
    /** Icon slot, ahead of the label. */
    lead?: Snippet;
    children?: Snippet;
    /** Merged with the chip's own classes rather than replacing them. */
    class?: string;
    /** Everything else (aria-*, title, data-*) lands on the rendered element. */
    [key: string]: unknown;
  }

  let {
    label = "",
    tone = "neutral",
    size = "sm",
    fill = "filled",
    active = false,
    toned = false,
    ring = false,
    pip = false,
    dense = false,
    disabled = false,
    href = null,
    onclick,
    lead,
    children,
    class: extraClass = "",
    ...rest
  }: Props = $props();

  const interactive = $derived(Boolean(href) || Boolean(onclick));
  const classes = $derived(
    [
      "chip",
      size,
      fill,
      tone,
      active ? "active" : "",
      toned ? "toned" : "",
      ring ? "ring" : "",
      dense ? "dense" : "",
      interactive ? "interactive" : "",
      extraClass
    ]
      .filter(Boolean)
      .join(" ")
  );
</script>

{#snippet body()}
  {#if pip}
    <span class="chip-pip" aria-hidden="true"></span>
  {/if}
  {#if lead}
    <span class="chip-lead" aria-hidden="true">{@render lead()}</span>
  {/if}
  {#if children}
    {@render children()}
  {:else if label}
    <span class="chip-label">{label}</span>
  {/if}
{/snippet}

<!-- Callers own description, the primitive owns behaviour: `rest` is spread
     first, so everything the chip is answerable for — the shape, the `rel` that
     makes `target="_blank"` safe, whether it is a submit button — is written
     after it and wins. IconButton spreads in the same order, for the same
     reason. A behaviour a caller legitimately needs gets a named prop there
     rather than being left overridable here. -->
{#if href}
  <a {...rest} class={classes} {href} target="_blank" rel="noreferrer noopener">
    {@render body()}
  </a>
{:else if onclick}
  <button {...rest} class={classes} type="button" {disabled} {onclick}>
    {@render body()}
  </button>
{:else}
  <span {...rest} class={classes}>
    {@render body()}
  </span>
{/if}

<style>
  .chip {
    --chip-color: var(--text-3);
    position: relative;
    display: inline-flex;
    align-items: center;
    min-width: 0;
    box-sizing: border-box;
    border: 1px solid transparent;
    color: color-mix(in srgb, var(--chip-color) 82%, white);
    font-family: var(--font-mono);
    font-size: var(--label-size);
    font-weight: var(--label-weight);
    letter-spacing: var(--label-tracking);
    text-transform: uppercase;
    text-decoration: none;
    line-height: 1;
    white-space: nowrap;
  }

  /* --- rows ---------------------------------------------------------------- */
  /* Only the box changes between the two rows. The label is the register at
     both, so a badge in a pane and a chip in the topbar are the same words at
     the same size — the height alone says which frame you are in. */
  .chip.xs {
    height: var(--control-height-xs);
    gap: 5px;
    padding: 0 7px;
  }

  .chip.sm {
    height: var(--control-height);
    gap: 6px;
    padding: 0 8px;
  }

  .chip.dense {
    gap: 5px;
    padding: 0 6px;
  }

  /* --- fills --------------------------------------------------------------- */
  .chip.filled {
    border-color: color-mix(in srgb, var(--chip-color) 40%, var(--border));
    background: color-mix(in srgb, var(--chip-color) 10%, var(--surface-1));
    box-shadow: var(--shadow-inset-mid);
  }

  .chip.quiet {
    border-color: var(--border);
    background: transparent;
    color: var(--text-3);
  }

  .chip.bare {
    border-color: transparent;
    background: transparent;
    color: var(--text-3);
  }

  /* Hue without chrome: the box stays neutral, the text carries the tone. */
  .chip.quiet.toned,
  .chip.bare.toned {
    color: var(--chip-color);
  }

  .chip.quiet.active,
  .chip.bare.active {
    border-color: color-mix(in srgb, var(--chip-color) 45%, transparent);
    background: color-mix(in srgb, var(--chip-color) 13%, var(--surface-2));
    color: color-mix(in srgb, var(--chip-color) 82%, white);
  }

  .chip.ring::after {
    content: "";
    position: absolute;
    inset: -1px;
    border: 1px solid color-mix(in srgb, var(--chip-color) 38%, transparent);
    opacity: 0.7;
    pointer-events: none;
  }

  /* --- interaction --------------------------------------------------------- */
  .chip.interactive {
    cursor: pointer;
    transition:
      color var(--duration-fast) var(--ease-out),
      border-color var(--duration-fast) var(--ease-out),
      background var(--duration-fast) var(--ease-out),
      transform var(--duration-press) var(--ease-out);
  }

  .chip.interactive:active:not(:disabled) {
    transform: scale(0.97);
  }

  .chip.interactive:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 1px;
  }

  .chip.interactive:disabled {
    opacity: var(--disabled-opacity);
    cursor: default;
  }

  @media (hover: hover) and (pointer: fine) {
    .chip.interactive:hover:not(:disabled) {
      color: var(--text-1);
      border-color: var(--border-strong);
    }

    .chip.filled.interactive:hover:not(:disabled) {
      border-color: color-mix(in srgb, var(--chip-color) 55%, var(--border));
      background: color-mix(in srgb, var(--chip-color) 16%, var(--surface-1));
    }

    /* A toned chip answers hover with its box, not by dropping its hue. */
    .chip.toned.interactive:hover:not(:disabled) {
      color: var(--chip-color);
      border-color: color-mix(in srgb, var(--chip-color) 45%, var(--border));
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .chip.interactive {
      transition: none;
    }

    .chip.interactive:active:not(:disabled) {
      transform: none;
    }
  }

  /* --- parts --------------------------------------------------------------- */
  .chip-pip {
    flex: 0 0 auto;
    width: var(--pip);
    height: var(--pip);
    background: var(--chip-color);
  }

  .chip-lead {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
  }

  .chip-label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* --- tones -------------------------------------------------------------- */
  .chip.accent { --chip-color: var(--accent); }
  .chip.model { --chip-color: var(--model); }
  .chip.reasoning { --chip-color: var(--reasoning); }
  .chip.tool { --chip-color: var(--warning); }
  .chip.retry { --chip-color: var(--retry); }
  .chip.queue { --chip-color: var(--queue); }
  .chip.success { --chip-color: var(--success); }
  .chip.error { --chip-color: var(--error); }
  .chip.live { --chip-color: var(--live); }
</style>
