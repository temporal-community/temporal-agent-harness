<script lang="ts">
  import { Handle, Position } from "@xyflow/svelte";
  import type { AgentNodeData } from "$lib/state/flowProjection";

  interface Props {
    data: AgentNodeData;
  }

  let { data }: Props = $props();

  const boundaryWidth = $derived(
    typeof data.boundaryWidth === "number" ? data.boundaryWidth : 930
  );
  const boundaryHeight = $derived(
    typeof data.boundaryHeight === "number" ? data.boundaryHeight : 240
  );
  const headerPrefix = $derived(
    typeof data.headerPrefix === "string" ? data.headerPrefix : null
  );
  const interfaces = $derived(
    Array.isArray(data.interfaces) ? data.interfaces : []
  );
  const visibleInterfaces = $derived(interfaces.slice(0, 2));
  const hiddenInterfaces = $derived(interfaces.slice(visibleInterfaces.length));
  const hiddenInterfaceCount = $derived(
    hiddenInterfaces.length
  );
  const hiddenInterfaceTitle = $derived(
    hiddenInterfaces
      .map((item) => item.description ? `${item.name}: ${item.description}` : item.name)
      .join("\n")
  );
  const runtimeRole = $derived(
    data.runtimeRole === "subagent" ? "subagent" : "parent"
  );
</script>

<div
  class={`workflow-boundary ${runtimeRole}`}
  style={`width: ${boundaryWidth}px; height: ${boundaryHeight}px;`}
>
  <Handle id="target-top" class="workflow-handle" type="target" position={Position.Top} />
  <div class="workflow-head">
    <div class="workflow-title-group">
      <span class="workflow-title">{data.title}</span>
      {#if data.subtitle}
        <span class="workflow-subtitle">{data.subtitle}</span>
      {/if}
      {#if visibleInterfaces.length}
        <div class="interfaces">
          <span class="interfaces-label">accepts</span>
          {#each visibleInterfaces as item}
            <span class="interface-chip" title={item.description}>{item.name}</span>
          {/each}
          {#if hiddenInterfaceCount > 0}
            <span class="interface-chip muted" title={hiddenInterfaceTitle}>+{hiddenInterfaceCount}</span>
          {/if}
        </div>
      {/if}
    </div>
    <span class="workflow-status">
      {#if headerPrefix}
        <span>{headerPrefix}</span>
      {/if}
      <strong>{data.state}</strong>
    </span>
  </div>
  <Handle id="source-bottom" class="workflow-handle" type="source" position={Position.Bottom} />
</div>

<style>
  .workflow-boundary {
    pointer-events: none;
    border: 1px solid color-mix(in srgb, var(--accent) 36%, var(--border));
    border-radius: 10px;
    background:
      linear-gradient(
        180deg,
        color-mix(in srgb, var(--surface-1) 88%, var(--accent) 12%),
        color-mix(in srgb, var(--surface-2) 90%, var(--accent) 10%)
      );
    box-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--surface-0) 64%, transparent),
      0 18px 44px rgba(0, 0, 0, 0.22);
  }

  .workflow-boundary.parent .workflow-head {
    background: color-mix(in srgb, var(--surface-0) 28%, transparent);
  }

  .workflow-boundary.subagent {
    border-color: color-mix(in srgb, var(--accent) 42%, var(--border-strong));
    background:
      linear-gradient(
        180deg,
        color-mix(in srgb, var(--surface-1) 92%, var(--accent) 8%),
        color-mix(in srgb, var(--surface-2) 88%, var(--accent) 12%)
      );
    box-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--surface-0) 70%, transparent),
      0 14px 36px rgba(0, 0, 0, 0.24);
  }

  .workflow-boundary.subagent .workflow-head {
    background: color-mix(in srgb, var(--surface-0) 34%, transparent);
    border-bottom-color: color-mix(in srgb, var(--accent) 30%, var(--border));
  }

  :global(.workflow-handle) {
    width: 1px;
    height: 1px;
    border: 0;
    opacity: 0;
    pointer-events: none;
  }

  .workflow-head {
    min-height: 88px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
    padding: 13px 14px 11px;
    border-bottom: 1px solid color-mix(in srgb, var(--accent) 22%, var(--border));
    color: var(--text-1);
    font-size: 12px;
    font-weight: 700;
  }

  .workflow-title-group {
    min-width: 0;
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 6px;
  }

  .workflow-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .workflow-subtitle {
    overflow: hidden;
    color: var(--text-3);
    font-size: 11px;
    font-weight: 600;
    line-height: 1.3;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .workflow-head strong {
    color: var(--accent);
    font-size: 11px;
    font-weight: 650;
  }

  .workflow-status {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: var(--text-3);
    font-size: 11px;
    white-space: nowrap;
  }

  .interfaces {
    pointer-events: auto;
    display: flex;
    align-items: center;
    gap: 5px;
    min-width: 0;
  }

  .interfaces-label {
    color: var(--text-3);
    font-size: 10px;
    font-weight: 650;
    text-transform: uppercase;
  }

  .interface-chip {
    min-width: 0;
    max-width: 96px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    padding: 2px 7px;
    border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
    border-radius: 999px;
    background: color-mix(in srgb, var(--accent) 10%, transparent);
    color: color-mix(in srgb, var(--accent) 74%, white);
    font-size: 10px;
    font-weight: 650;
  }

  .interface-chip.muted {
    max-width: none;
    color: var(--text-3);
    background: color-mix(in srgb, var(--text-3) 8%, transparent);
    border-color: color-mix(in srgb, var(--text-3) 20%, var(--border));
  }
</style>
