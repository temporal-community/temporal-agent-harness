<script lang="ts">
  import { Bot, Network, Sparkles } from "@lucide/svelte";

  interface Props {
    label: string;
    workflowType?: string | null;
    status?: "available" | "busy" | "approval" | "error" | "idle";
    size?: "sm" | "md" | "lg";
    role?: "agent" | "subagent" | "tool";
  }

  let {
    label,
    workflowType = null,
    status = "idle",
    size = "md",
    role = "agent"
  }: Props = $props();

  const seed = $derived(workflowType || label);
  const variant = $derived(hashString(seed) % 6);
  const initial = $derived((label.trim()[0] ?? "A").toUpperCase());

  function hashString(value: string): number {
    let hash = 0;
    for (let index = 0; index < value.length; index += 1) {
      hash = (hash * 31 + value.charCodeAt(index)) >>> 0;
    }
    return hash;
  }
</script>

<span
  class={`agent-glyph ${size} ${role} ${status} v${variant}`}
  aria-hidden="true"
  title={label}
>
  {#if role === "subagent"}
    <Network size={size === "lg" ? 18 : size === "sm" ? 12 : 15} />
  {:else if role === "tool"}
    <Sparkles size={size === "lg" ? 18 : size === "sm" ? 12 : 15} />
  {:else}
    <Bot size={size === "lg" ? 18 : size === "sm" ? 12 : 15} />
  {/if}
  <span class="agent-initial">{initial}</span>
  <span class="agent-status" aria-hidden="true"></span>
</span>

<style>
  .agent-glyph {
    --glyph-color: var(--accent);
    position: relative;
    display: inline-grid;
    place-items: center;
    flex: 0 0 auto;
    border: 1px solid color-mix(in srgb, var(--glyph-color) 42%, var(--border));
    border-radius: 7px;
    color: color-mix(in srgb, var(--glyph-color) 76%, white);
    background:
      linear-gradient(
        135deg,
        color-mix(in srgb, var(--glyph-color) 22%, var(--surface-2)),
        color-mix(in srgb, var(--surface-1) 82%, black)
      );
    box-shadow:
      inset 0 1px 0 rgb(255 255 255 / 0.07),
      0 0 0 1px rgb(255 255 255 / 0.02);
  }

  .agent-glyph.sm {
    width: 24px;
    height: 24px;
    border-radius: 6px;
  }

  .agent-glyph.md {
    width: 30px;
    height: 30px;
  }

  .agent-glyph.lg {
    width: 38px;
    height: 38px;
    border-radius: 8px;
  }

  .agent-glyph.v1 { --glyph-color: var(--reasoning); }
  .agent-glyph.v2 { --glyph-color: var(--model); }
  .agent-glyph.v3 { --glyph-color: var(--warning); }
  .agent-glyph.v4 { --glyph-color: #ff9f7a; }
  .agent-glyph.v5 { --glyph-color: var(--queue); }

  .agent-glyph :global(svg) {
    opacity: 0.72;
  }

  .agent-initial {
    position: absolute;
    right: 4px;
    bottom: 2px;
    color: var(--text-1);
    font-size: 8px;
    font-weight: 800;
    line-height: 1;
  }

  .agent-glyph.sm .agent-initial {
    right: 3px;
    bottom: 2px;
    font-size: 7px;
  }

  .agent-glyph.lg .agent-initial {
    right: 5px;
    bottom: 3px;
    font-size: 9px;
  }

  .agent-status {
    position: absolute;
    right: -3px;
    bottom: -3px;
    width: 8px;
    height: 8px;
    border: 2px solid var(--surface-1);
    border-radius: 999px;
    background: var(--text-3);
  }

  .agent-glyph.available .agent-status { background: var(--success); }
  .agent-glyph.busy .agent-status { background: var(--accent); }
  .agent-glyph.approval .agent-status { background: var(--queue); }
  .agent-glyph.error .agent-status { background: var(--error); }
</style>
