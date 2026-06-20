<script lang="ts">
  interface Props {
    label: string;
    tone?:
      | "neutral"
      | "agent"
      | "model"
      | "reasoning"
      | "tool"
      | "approval"
      | "done"
      | "error"
      | "queue";
  }

  let { label, tone = "neutral" }: Props = $props();
</script>

<span class={`badge ${tone}`}>{label}</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    min-height: 20px;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid var(--border);
    color: var(--text-2);
    background: var(--surface-2);
    font-size: 11px;
    line-height: 1.25;
    white-space: nowrap;
  }

  .badge.agent { color: var(--accent); }
  .badge.model { color: var(--model); }
  .badge.reasoning { color: var(--reasoning); }
  .badge.tool { color: var(--warning); }
  .badge.approval,
  .badge.queue { color: var(--queue); }
  .badge.done { color: var(--success); }
  .badge.error { color: var(--error); }
</style>
