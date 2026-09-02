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
    pressed?: boolean;
    tone?: "default" | "primary" | "follow";
    onclick?: (event: MouseEvent) => void;
    children?: Snippet;
  }

  let {
    label,
    tip = label,
    disabled = false,
    pressed = false,
    tone = "default",
    onclick,
    children
  }: Props = $props();
</script>

<!-- `data-tip` rather than `title`: the browser waits about a second on one
     element before showing a `title`, so a row of transport buttons the pointer
     sweeps across never says anything at all. `aria-label` keeps the name. -->
<button
  class={`icon-button ${tone} ${pressed ? "pressed" : ""}`}
  type="button"
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
