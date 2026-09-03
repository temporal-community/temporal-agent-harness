<script lang="ts">
  import type { Snippet } from "svelte";

  interface Props {
    label: string;
    /**
     * The hover hint. Defaults to the accessible name, which is the whole point
     * of an icon-only control: the word it does not show, shown on hover.
     * Name the keyboard shortcut here too where one exists — an icon that has
     * to be discovered by hovering may as well teach the faster way at the same
     * time.
     */
    tip?: string;
    disabled?: boolean;
    /**
     * Left undefined for the buttons that are not toggles, so they do not
     * announce themselves as an unpressed one. A toggle passes it either way
     * and gets both states.
     */
    pressed?: boolean;
    tone?: "default" | "primary" | "follow";
    /**
     * `submit` is the one behaviour a caller may change, and it is a named prop
     * rather than something reachable through `rest` because it decides what
     * the button *does* — a composer needs it to keep native Enter-to-send.
     */
    type?: "button" | "submit";
    onclick?: (event: MouseEvent) => void;
    children?: Snippet;
    /** Merged with the button's own classes rather than replacing them. */
    class?: string;
    /** Everything else (aria-*, title, data-*) lands on the rendered element. */
    [key: string]: unknown;
  }

  let {
    label,
    tip = label,
    disabled = false,
    pressed,
    tone = "default",
    type = "button",
    onclick,
    children,
    class: extraClass = "",
    ...rest
  }: Props = $props();
</script>

<!-- `data-tip` rather than `title`: the browser waits about a second on one
     element before showing a `title`, so a row of transport buttons the pointer
     sweeps across never says anything at all. `aria-label` keeps the name. -->
<!-- Callers own description, the primitive owns behaviour: `rest` is spread
     first, so everything this button is answerable for is written after it and
     wins. Chip spreads in the same order, for the same reason. `class` is the
     one attribute that must not follow that rule — a caller adding one has
     nothing to say about `type` — so it is pulled out of `rest` and merged
     rather than overridden. -->
<button
  {...rest}
  class={["icon-button", tone, pressed && "pressed", extraClass]}
  {type}
  aria-label={label}
  aria-pressed={pressed}
  data-tip={tip}
  {disabled}
  onclick={(event) => onclick?.(event)}
>
  {@render children?.()}
</button>

<style>
  .icon-button {
    width: var(--control-height);
    height: var(--control-height);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    color: var(--text-2);
    cursor: pointer;
    transition:
      transform var(--duration-press) var(--ease-out),
      color var(--duration-fast) var(--ease-ui),
      border-color var(--duration-fast) var(--ease-ui),
      background var(--duration-fast) var(--ease-ui);
  }

  @media (hover: hover) and (pointer: fine) {
    .icon-button:hover:not(:disabled) {
      color: var(--text-1);
      border-color: var(--border-strong);
      background: var(--surface-3);
    }
  }

  .icon-button:active:not(:disabled) {
    transform: scale(0.97);
  }

  .icon-button.primary,
  .icon-button.pressed {
    color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 45%, transparent);
    background: color-mix(in srgb, var(--accent) 13%, var(--surface-2));
  }

  /* Named for what it does, not for the hue it borrows: --live is reserved for
     work that needs a human, and tailing the stream does not. */
  .icon-button.follow {
    color: var(--success);
    border-color: color-mix(in srgb, var(--success) 45%, transparent);
  }

  .icon-button:disabled {
    opacity: var(--disabled-opacity);
    cursor: default;
  }

  @media (prefers-reduced-motion: reduce) {
    .icon-button:active:not(:disabled) {
      transform: none;
    }
  }
</style>
