<script lang="ts">
  interface LegendItem {
    label: string;
    tone: string;
  }

  interface Props {
    compact?: boolean;
  }

  let { compact = false }: Props = $props();

  const items: LegendItem[] = [
    { label: "agent", tone: "agent" },
    { label: "model", tone: "model" },
    { label: "tool", tone: "tool" },
    { label: "approval / queue", tone: "queue" },
    { label: "done", tone: "done" },
    { label: "error", tone: "error" }
  ];
</script>

<div class={`legend ${compact ? "compact" : ""}`} aria-label="Color legend">
  {#each items as item}
    <span class={`legend-item ${item.tone}`}>
      <span class="swatch" aria-hidden="true"></span>
      {item.label}
    </span>
  {/each}
</div>

<style>
  .legend {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 12px;
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: color-mix(in srgb, var(--surface-1) 92%, transparent);
    backdrop-filter: blur(8px);
  }

  .legend.compact {
    gap: 3px 10px;
    padding: 5px 8px;
  }

  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-3);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    white-space: nowrap;
  }

  .swatch {
    width: 9px;
    height: 9px;
    border-radius: 3px;
    background: var(--text-3);
  }

  .agent .swatch { background: var(--accent); }
  .model .swatch { background: var(--model); }
  .tool .swatch { background: var(--warning); }
  .queue .swatch { background: var(--queue); }
  .done .swatch { background: var(--success); }
  .error .swatch { background: var(--error); }
</style>
