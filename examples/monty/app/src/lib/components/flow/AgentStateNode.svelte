<script lang="ts">
  import { Handle, Position } from "@xyflow/svelte";
  import type { AgentNodeData } from "$lib/state/flowProjection";
  import Badge from "$lib/components/primitives/Badge.svelte";

  interface Props {
    data: AgentNodeData;
    selected?: boolean;
  }

  let { data, selected = false }: Props = $props();

  function scriptFromDetail(detail: unknown): string | null {
    if (typeof detail !== "string") return null;
    try {
      const parsed = JSON.parse(detail);
      if (typeof parsed?.script === "string") return parsed.script;
      if (typeof parsed?.payload?.script === "string") return parsed.payload.script;
    } catch {
      return null;
    }
    return null;
  }

  const nodeStyle = $derived(
    [
      typeof data.nodeWidth === "number" ? `width: ${data.nodeWidth}px;` : null,
      typeof data.nodeHeight === "number" ? `min-height: ${data.nodeHeight}px;` : null
    ]
      .filter(Boolean)
      .join(" ")
  );
  const detailScript = $derived(scriptFromDetail(data.detail));
</script>

<div
  class={`state-node ${data.tone} ${data.size ?? "default"} ${data.active ? "active" : ""} ${selected ? "selected" : ""}`}
  style={nodeStyle}
>
  <Handle id="target-left" class="node-handle" type="target" position={Position.Left} />
  <Handle id="target-top" class="node-handle" type="target" position={Position.Top} />
  <Handle id="target-bottom" class="node-handle" type="target" position={Position.Bottom} />
  {#if data.approvalPort}
    <Handle
      id="approval-out"
      class="node-handle approval-port tool-approval-port"
      type="source"
      position={Position.Left}
    />
  {/if}
  {#if data.approvalDecisionPort}
    <Handle
      id="approval-request-in"
      class="node-handle approval-port decision-approval-port"
      type="target"
      position={Position.Right}
    />
  {/if}
  <div class="topline">
    <span class="title-wrap">
      {#if data.dotTone}
        <span class={`title-dot ${data.dotTone}`} aria-hidden="true"></span>
      {/if}
      <span class="title">{data.title}</span>
    </span>
    <Badge label={data.state} tone={data.statusTone ?? data.tone} />
  </div>
  {#if data.subtitle}
    <div class="subtitle">{data.subtitle}</div>
  {/if}
  {#if detailScript}
    <pre class="detail code-detail"><code>{detailScript}</code></pre>
  {:else if data.detail}
    <div class="detail">{data.detail}</div>
  {/if}
  {#if data.metrics?.length}
    <div class="metrics">
      {#each data.metrics as metric}
        <span><strong>{metric.value}</strong>{metric.label}</span>
      {/each}
    </div>
  {/if}
  <Handle id="source-right" class="node-handle" type="source" position={Position.Right} />
  <Handle id="source-top" class="node-handle" type="source" position={Position.Top} />
  <Handle id="source-bottom" class="node-handle" type="source" position={Position.Bottom} />
</div>

<style>
  .state-node {
    width: 230px;
    min-height: 96px;
    padding: 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: color-mix(in srgb, var(--tone-color, var(--text-2)) 8%, var(--surface-2));
    color: var(--text-1);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
  }

  .state-node.selected {
    outline: 2px solid color-mix(in srgb, var(--accent) 65%, transparent);
  }

  .state-node.active {
    border-color: color-mix(in srgb, var(--tone-color, var(--accent)) 72%, white 8%);
    box-shadow:
      0 0 0 1px color-mix(in srgb, var(--tone-color, var(--accent)) 30%, transparent),
      0 0 22px color-mix(in srgb, var(--tone-color, var(--accent)) 42%, transparent),
      0 18px 42px rgba(0, 0, 0, 0.34);
  }

  .state-node.large {
    width: 255px;
    min-height: 112px;
  }

  .state-node.container {
    background: color-mix(in srgb, var(--tone-color, var(--warning)) 12%, transparent);
    box-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--tone-color, var(--warning)) 18%, transparent),
      0 18px 42px rgba(0, 0, 0, 0.22);
  }

  .state-node.container .detail {
    max-height: 34px;
  }

  .neutral { --tone-color: var(--text-3); }
  .agent { --tone-color: var(--accent); }
  .model { --tone-color: var(--model); }
  .reasoning { --tone-color: var(--reasoning); }
  .tool { --tone-color: var(--warning); }
  .approval,
  .queue { --tone-color: var(--queue); }
  .done { --tone-color: var(--success); }
  .error { --tone-color: var(--error); }

  .topline {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
  }

  .title {
    font-size: 13px;
    font-weight: 700;
  }

  .title-wrap {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }

  .title-dot {
    flex: 0 0 auto;
    width: 8px;
    height: 8px;
    border-radius: 999px;
    background: var(--text-3);
    box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 10%, transparent);
  }

  .title-dot.model { background: var(--model); }
  .title-dot.reasoning { background: var(--reasoning); }
  .title-dot.tool { background: var(--warning); }
  .title-dot.approval { background: var(--queue); }

  .state-node.model.active {
    border-color: color-mix(in srgb, var(--model) 72%, white 8%);
    box-shadow:
      0 0 0 1px color-mix(in srgb, var(--model) 30%, transparent),
      0 0 24px color-mix(in srgb, var(--model) 48%, transparent),
      0 18px 42px rgba(0, 0, 0, 0.34);
  }

  :global(.node-handle) {
    width: 1px;
    height: 1px;
    border: 0;
    opacity: 0;
    pointer-events: none;
  }

  :global(.node-handle.approval-port) {
    width: 9px;
    height: 9px;
    border: 1px solid color-mix(in srgb, var(--queue) 76%, white 10%);
    background: var(--queue);
    box-shadow:
      0 0 0 3px color-mix(in srgb, var(--queue) 18%, transparent),
      0 0 16px color-mix(in srgb, var(--queue) 44%, transparent);
    opacity: 1;
  }

  :global(.node-handle.tool-approval-port) {
    top: 72px;
  }

  .subtitle {
    margin-top: 7px;
    color: var(--text-2);
    font-size: 12px;
    line-height: 1.35;
    word-break: break-word;
  }

  .detail {
    margin-top: 8px;
    max-height: 58px;
    overflow: hidden;
    color: var(--text-3);
    font-size: 11px;
    line-height: 1.35;
    word-break: break-word;
  }

  .code-detail {
    max-height: 132px;
    padding: 8px;
    border: 1px solid color-mix(in srgb, var(--border) 74%, transparent);
    border-radius: 6px;
    background: color-mix(in srgb, var(--surface-0) 72%, black 8%);
    color: var(--text-2);
    font-family:
      ui-monospace,
      SFMono-Regular,
      Menlo,
      Monaco,
      Consolas,
      "Liberation Mono",
      monospace;
    white-space: pre;
    word-break: normal;
  }

  .metrics {
    display: flex;
    gap: 8px;
    margin-top: 10px;
    color: var(--text-3);
    font-size: 10px;
    text-transform: uppercase;
  }

  .metrics span {
    display: inline-flex;
    flex-direction: column;
    gap: 2px;
  }

  .metrics strong {
    color: var(--text-1);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }
</style>
