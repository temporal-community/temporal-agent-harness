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
    --badge-color: var(--text-3);
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: 22px;
    padding: 2px 8px 2px 7px;
    border-radius: 6px;
    border: 1px solid color-mix(in srgb, var(--badge-color) 28%, var(--border));
    color: color-mix(in srgb, var(--badge-color) 78%, white);
    background: color-mix(in srgb, var(--badge-color) 9%, var(--surface-1));
    font-size: 11px;
    font-weight: 700;
    line-height: 1.25;
    white-space: nowrap;
    box-shadow: inset 0 1px 0 rgb(255 255 255 / 0.04);
  }

  .badge::before {
    content: "";
    width: 5px;
    height: 5px;
    flex: 0 0 auto;
    border-radius: 999px;
    background: var(--badge-color);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--badge-color) 16%, transparent);
  }

  .badge.agent { --badge-color: var(--accent); }
  .badge.model { --badge-color: var(--model); }
  .badge.reasoning { --badge-color: var(--reasoning); }
  .badge.tool { --badge-color: var(--warning); }
  .badge.approval,
  .badge.queue { --badge-color: var(--queue); }
  .badge.done { --badge-color: var(--success); }
  .badge.error { --badge-color: var(--error); }
</style>
