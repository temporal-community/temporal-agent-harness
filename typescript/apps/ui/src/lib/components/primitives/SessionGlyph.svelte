<script lang="ts" module>
  import type { StatusKind } from "$lib/components/primitives/StatusChip.svelte";

  /* The glyph's shape says the state on its own; these only tint it. */
  export const ROW_GLYPH_KINDS = new Set<StatusKind>(["approval", "stuck", "blocked", "error"]);
</script>

<script lang="ts">
  /* A status's shape wherever a session is drawn: its row, its card, the nav and search. A gone
     workflow takes StatusChip's closed circle in the quiet closed tone: like a close, nothing
     more can happen to it, but it is no longer there to open, so not the plain closed pip. */
  import { XCircle } from "@lucide/svelte";
  import StateGlyph from "$lib/components/primitives/StateGlyph.svelte";
  import { STATUS_TONES } from "$lib/components/primitives/StatusChip.svelte";

  let { kind, unavailable = false }: { kind: StatusKind; unavailable?: boolean } = $props();

  const color = $derived(kind === "closed" ? "var(--text-4)" : STATUS_TONES[kind] === "neutral" ? "var(--text-3)" : `var(--${STATUS_TONES[kind]})`);
</script>

<span class="session-glyph" aria-hidden="true" style:--status-color={color}>
  {#if unavailable}<XCircle size={12} aria-hidden="true" />{:else if ROW_GLYPH_KINDS.has(kind)}<StateGlyph {kind} size={12} />{:else}<span class={["session-pip", kind === "closed" && "hollow"]}></span>{/if}
</span>

<style>
  .session-glyph {
    flex: none;
    width: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--status-color);
  }

  .session-pip {
    width: var(--pip);
    height: var(--pip);
    background: currentColor;
  }

  .session-pip.hollow {
    background: transparent;
    box-shadow: inset 0 0 0 1px currentColor;
  }
</style>
