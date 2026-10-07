<script lang="ts" module>
  /**
   * False where there is no clipboard to write to — `navigator.clipboard` is
   * absent outside a secure context, so a console reached over plain http on
   * anything but localhost has none — or where the browser refused the write.
   */
  export async function copyText(value: string): Promise<boolean> {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return false;
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch {
      return false;
    }
  }
</script>

<script lang="ts">
  import { Check, Copy } from "@lucide/svelte";
  import type { Snippet } from "svelte";
  import IconButton from "./IconButton.svelte";

  interface Props {
    value: string;
    /** The accessible name, e.g. "Copy workflow ID". */
    label?: string;
    /** What the copy control sits beside; hovering or focusing any of it reveals the control. */
    children?: Snippet;
    /** Tip placement and the like (`data-tip-below`, `data-tip-align`), passed to the button. */
    [key: string]: unknown;
  }

  let { value, label = "Copy", children, ...rest }: Props = $props();

  let outcome = $state<"idle" | "copied" | "failed">("idle");
  let timer: ReturnType<typeof setTimeout> | undefined;

  async function copy(event: MouseEvent): Promise<void> {
    event.stopPropagation();
    outcome = (await copyText(value)) ? "copied" : "failed";
    clearTimeout(timer);
    timer = setTimeout(() => (outcome = "idle"), 1500);
  }

  $effect(() => () => clearTimeout(timer));

  const name = $derived(
    outcome === "copied" ? "Copied" : outcome === "failed" ? "Copy failed" : label
  );
</script>

<span class="copyable">
  {@render children?.()}
  <IconButton {...rest} label={name} class={outcome === "idle" ? "copy" : "copy shown"} onclick={copy}>
    {#if outcome === "copied"}
      <Check size={12} aria-hidden="true" />
    {:else}
      <Copy size={12} aria-hidden="true" />
    {/if}
  </IconButton>
</span>

<style>
  .copyable {
    display: inline-flex;
    align-items: center;
    gap: var(--gap-xs);
    min-width: 0;
    max-width: 100%;
  }

  /* A flex item will not shrink below its longest word, so an unbroken token in the text
     would carry the line, and the copy control after it, off the edge. */
  .copyable > :global(:not(.copy)) {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .copyable :global(.copy) {
    width: var(--control-height-xs);
    height: var(--control-height-xs);
    flex: none;
    opacity: 0;
  }

  /* Revealed by the pointer or the keyboard, and held while it reports. */
  .copyable:hover :global(.copy),
  .copyable:focus-within :global(.copy),
  .copyable :global(.copy.shown) {
    opacity: 1;
  }

  /* No hover to reveal it on a touch screen. */
  @media (hover: none) {
    .copyable :global(.copy) {
      opacity: 1;
    }
  }
</style>
