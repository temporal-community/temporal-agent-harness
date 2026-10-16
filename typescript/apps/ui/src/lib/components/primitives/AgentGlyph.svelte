<script lang="ts">
  import { Bot, Network } from "@lucide/svelte";

  /**
   * The glyph carries no identity hue. A name hashed into one of six event hues
   * was neither: six agents landed on four colours, so it did not identify, and
   * the colours it borrowed were already spoken for — a --reasoning glyph a few
   * hundred pixels above a --reasoning thought node, a --queue glyph on an agent
   * wearing a red error pip. The name is eight pixels away in 13px bold and stays
   * legible at zoom levels where a 14% tint has vanished, so the label does the
   * identifying and the pip stays the only colour on the glyph.
   */
  interface Props {
    label: string;
    status?: "available" | "busy" | "approval" | "error" | "idle";
    role?: "agent" | "subagent";
  }

  let { label, status = "idle", role = "agent" }: Props = $props();

  const initial = $derived((label.trim()[0] ?? "A").toUpperCase());
</script>

<span
  class={`agent-glyph ${role} ${status}`}
  aria-hidden="true"
  title={label}
>
  {#if role === "subagent"}
    <Network size={15} />
  {:else}
    <Bot size={15} />
  {/if}
  <span class="agent-initial">{initial}</span>
  <span class="agent-status" aria-hidden="true"></span>
</span>

<style>
  /* The frame sits at the same grey as the quietest text, so the status pip is
     the only colour on the glyph. */
  .agent-glyph {
    position: relative;
    display: inline-grid;
    place-items: center;
    flex: 0 0 auto;
    width: 30px;
    height: 30px;
    border: 1px solid color-mix(in srgb, var(--text-4) 42%, var(--border));
    border-radius: var(--radius-lg);
    color: color-mix(in srgb, var(--text-4) 76%, var(--text-1));
    background: color-mix(in srgb, var(--text-4) 14%, var(--surface-1));
    box-shadow:
      var(--shadow-inset-bright),
      var(--shadow-ring-faint);
  }

  .agent-glyph :global(svg) {
    opacity: 0.72;
  }

  .agent-initial {
    position: absolute;
    right: 4px;
    bottom: 2px;
    color: var(--text-1);
    font-size: var(--font-2xs);
    font-weight: 800;
    line-height: 1;
  }

  .agent-status {
    position: absolute;
    right: -3px;
    bottom: -3px;
    width: var(--pip-lg);
    height: var(--pip-lg);
    border: 2px solid var(--surface-1);
    background: var(--text-3);
  }

  .agent-glyph.available .agent-status { background: var(--success); }
  .agent-glyph.busy .agent-status { background: var(--accent); }
  /* The pip follows the approval chip into "this needs you". */
  .agent-glyph.approval .agent-status { background: var(--live); }
  .agent-glyph.error .agent-status { background: var(--error); }
</style>
