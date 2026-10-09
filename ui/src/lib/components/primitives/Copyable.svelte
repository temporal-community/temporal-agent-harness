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

  /** Selects a node's text so the reader can copy it by hand; false when there is none to select. */
  export function selectText(node: Node | null | undefined): boolean {
    const selection = node && typeof getSelection === "function" ? getSelection() : null;
    if (!node || !selection) return false;
    selection.selectAllChildren(node);
    return true;
  }

  /** How long each outcome holds. A failure is the one the reader has to act on, so it stays longer. */
  export const OUTCOME_MS = { copied: 1500, failed: 5000 } as const;

  /** The control's name, which is also what the live region announces. */
  export function outcomeLabel(
    outcome: "idle" | "copied" | "failed",
    label: string,
    selected: boolean,
    shortcut: string
  ): string {
    if (outcome === "copied") return "Copied";
    if (outcome === "failed") return selected ? `Copy failed — selected, press ${shortcut}` : "Copy failed";
    return label;
  }
</script>

<script lang="ts">
  import { Check, CircleAlert, Copy } from "@lucide/svelte";
  import type { Snippet } from "svelte";
  import IconButton from "./IconButton.svelte";

  type Selectable = Node | null | undefined;

  interface Props {
    value: string;
    /** The accessible name, e.g. "Copy workflow ID". */
    label?: string;
    /** What the copy control sits beside; hovering or focusing any of it reveals the control. */
    children?: Snippet;
    /**
     * What to select when the clipboard refuses, so Cmd/Ctrl+C still works. Defaults to the
     * text beside the control, but only when that text IS the value — a label is not.
     */
    select?: () => Selectable | Promise<Selectable>;
    /** Tip placement and the like (`data-tip-below`, `data-tip-align`), passed to the button. */
    [key: string]: unknown;
  }

  let { value, label = "Copy", children, select, ...rest }: Props = $props();

  let outcome = $state<"idle" | "copied" | "failed">("idle");
  let selected = $state(false);
  let wrapper = $state<HTMLElement | null>(null);
  let timer: ReturnType<typeof setTimeout> | undefined;

  const shortcut = $derived(
    typeof navigator !== "undefined" && /Mac|iP(hone|ad)/.test(navigator.platform) ? "⌘C" : "Ctrl+C"
  );

  function besideText(): Selectable {
    return wrapper?.textContent?.trim() === value.trim() ? wrapper : null;
  }

  async function copy(event: MouseEvent): Promise<void> {
    event.stopPropagation();
    clearTimeout(timer);
    outcome = "idle";
    const ok = await copyText(value);
    selected = !ok && selectText(await (select ?? besideText)());
    outcome = ok ? "copied" : "failed";
    timer = setTimeout(() => (outcome = "idle"), OUTCOME_MS[outcome]);
  }

  $effect(() => () => clearTimeout(timer));

  const name = $derived(outcomeLabel(outcome, label, selected, shortcut));
</script>

<span class="copyable" bind:this={wrapper}>
  {@render children?.()}
  <IconButton {...rest} label={name} class={outcome === "idle" ? "copy" : `copy shown ${outcome}`} onclick={copy}>
    {#if outcome === "copied"}
      <Check size={12} aria-hidden="true" />
    {:else if outcome === "failed"}
      <CircleAlert size={12} aria-hidden="true" />
    {:else}
      <Copy size={12} aria-hidden="true" />
    {/if}
  </IconButton>
</span>
<!-- Outside the wrapper, so it is never part of the text the fallback compares and selects. -->
<span class="announce" role="status">{outcome === "idle" ? "" : name}</span>

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

  .copyable :global(.copy.failed) {
    color: var(--error);
  }

  /* Read out, never drawn: the icon and the tip already say it to a sighted reader. */
  .announce {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  /* No hover to reveal it on a touch screen. */
  @media (hover: none) {
    .copyable :global(.copy) {
      opacity: 1;
    }
  }
</style>
