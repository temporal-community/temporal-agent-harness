<script lang="ts">
  import type { Snippet } from "svelte";

  interface Props {
    label: string;
    title?: string;
    disabled?: boolean;
    pressed?: boolean;
    tone?: "default" | "primary" | "live";
    onclick?: () => void;
    children?: Snippet;
  }

  let {
    label,
    title = label,
    disabled = false,
    pressed = false,
    tone = "default",
    onclick,
    children
  }: Props = $props();
</script>

<button
  class={`icon-button ${tone} ${pressed ? "pressed" : ""}`}
  type="button"
  aria-label={label}
  aria-pressed={pressed}
  {title}
  {disabled}
  onclick={() => onclick?.()}
>
  {@render children?.()}
</button>

<style>
  .icon-button {
    width: 34px;
    height: 34px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--surface-2);
    color: var(--text-2);
    cursor: pointer;
    transition:
      color 120ms ease,
      border-color 120ms ease,
      background 120ms ease;
  }

  .icon-button:hover:not(:disabled) {
    color: var(--text-1);
    border-color: var(--border-strong);
    background: var(--surface-3);
  }

  .icon-button.primary,
  .icon-button.pressed {
    color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 45%, transparent);
    background: color-mix(in srgb, var(--accent) 13%, var(--surface-2));
  }

  .icon-button.live {
    color: var(--success);
    border-color: color-mix(in srgb, var(--success) 45%, transparent);
  }

  .icon-button:disabled {
    opacity: 0.45;
    cursor: default;
  }
</style>
