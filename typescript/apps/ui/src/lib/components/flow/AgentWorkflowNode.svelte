<script lang="ts">
  import { Handle, Position } from "@xyflow/svelte";
  import { markdownToText } from "$lib/components/chat/MarkdownMessage.svelte";
  import type { AgentNodeData } from "$lib/state/flowProjection";
  import { kindFromState } from "$lib/components/flow/AgentStateNode.svelte";
  import StateGlyph from "$lib/components/primitives/StateGlyph.svelte";
  import StatusChip, {
    STATUS_TONES,
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";

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
  const hiddenInterfaceTitle = $derived(
    hiddenInterfaces
      .map((item) => item.description ? `${item.name}: ${markdownToText(item.description)}` : item.name)
      .join("\n")
  );
  const runtimeRole = $derived(
    data.runtimeRole === "subagent" ? "subagent" : "parent"
  );
  /* The cards' own reading, so a boundary, its inspector chip and its minimap mark agree.
     A running subagent is its parent delegating, not thinking. */
  const cardKind = $derived(kindFromState(data.state, data.tone));
  const statusKind: StatusKind = $derived(
    runtimeRole === "subagent" && cardKind === "thinking" ? "delegating" : cardKind
  );
  const running = $derived(statusKind === "thinking" || statusKind === "delegating");
  /* Measured by length, not layout; see the same call in AgentStateNode. */
  const titleTip = $derived(data.title.length > 40 ? data.title : undefined);
</script>

<div
  class={["workflow-boundary", runtimeRole, `status-${STATUS_TONES[statusKind]}`]}
  style={`width: ${boundaryWidth}px; height: ${boundaryHeight}px;`}
>
  <Handle id="target-top" class="workflow-handle" type="target" position={Position.Top} />
  <div class="workflow-head">
    <div class="workflow-title-group">
      <span class="workflow-title-row" data-tip={titleTip} data-tip-below>
        <StateGlyph kind={statusKind} {running} />
        <span class="workflow-title" dir="auto">{data.title}</span>
      </span>
      {#if data.reason}
        <!-- Shown, not a tip, which a keyboard or touch cannot reach. Two lines is what
             runtimeReasonHeight in flowProjection reserves; the inspector has it in full. -->
        <span class="workflow-reason" dir="auto">{markdownToText(data.reason)}</span>
      {/if}
      {#if data.subtitle}
        <span class="workflow-subtitle" dir="auto">{data.subtitle}</span>
      {/if}
      {#if visibleInterfaces.length}
        <div class="interfaces">
          <span class="kicker">accepts</span>
          {#each visibleInterfaces as item (item)}
            <code class="handler" data-tip={markdownToText(item.description) || undefined}>{item.name}</code>
          {/each}
          {#if hiddenInterfaces.length > 0}
            <span class="handler more" data-tip={hiddenInterfaceTitle}>+{hiddenInterfaces.length}</span>
          {/if}
        </div>
      {/if}
    </div>
    <span class="workflow-status">
      {#if headerPrefix}
        <span>{headerPrefix}</span>
      {/if}
      <StatusChip label={data.state} kind={statusKind} plain />
    </span>
  </div>
  <Handle id="source-bottom" class="workflow-handle" type="source" position={Position.Bottom} />
</div>

<style>
  .workflow-boundary {
    --status-color: var(--accent);
    pointer-events: none;
    border: 1px solid color-mix(in srgb, var(--accent) 36%, var(--border));
    border-radius: var(--radius-lg);
    background: color-mix(in srgb, var(--surface-1) 90%, var(--accent) 10%);
    box-shadow: var(--shadow-node-tall);
  }

  .workflow-boundary.subagent {
    border-color: color-mix(in srgb, var(--accent) 42%, var(--border-strong));
    background: color-mix(in srgb, var(--surface-1) 92%, var(--accent) 8%);
    box-shadow: var(--shadow-node-mid);
  }

  .status-neutral { --status-color: var(--text-3); }
  .status-accent { --status-color: var(--accent); }
  .status-reasoning { --status-color: var(--reasoning); }
  .status-success { --status-color: var(--success); }
  .status-error { --status-color: var(--error); }
  .status-live { --status-color: var(--live); }

  /* Zero-sized for the same reason as AgentStateNode's .node-handle. */
  :global(.workflow-handle) {
    width: 0;
    height: 0;
    min-width: 0;
    min-height: 0;
    margin: -1px;
    border: 0;
    opacity: 0;
    pointer-events: none;
  }

  .workflow-head {
    min-height: 88px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    padding: 13px 14px 11px;
    border-bottom: 1px solid color-mix(in srgb, var(--status-color) 26%, var(--border));
    /* No more than 8%: --text-3 on a 12% green band measured 4.36:1. */
    background: color-mix(in srgb, var(--status-color) 8%, var(--surface-1));
    color: var(--text-1);
    font-size: var(--font-md);
    font-weight: 700;
  }

  .workflow-title-group {
    min-width: 0;
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 6px;
  }

  .workflow-title-row {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: var(--gap-sm);
    pointer-events: auto;
  }

  .workflow-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .workflow-reason {
    display: -webkit-box;
    overflow: hidden;
    color: var(--text-3);
    font-size: var(--font-sm);
    font-weight: 500;
    line-height: 1.35;
    overflow-wrap: anywhere;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  .workflow-subtitle {
    overflow: hidden;
    color: var(--text-3);
    font-size: var(--font-sm);
    font-weight: 600;
    line-height: 1.3;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .workflow-status {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-3);
    font-size: var(--font-sm);
    white-space: nowrap;
  }

  .interfaces {
    pointer-events: auto;
    display: flex;
    align-items: center;
    gap: var(--gap-md);
    min-width: 0;
  }

  /* Names, not controls: nothing here can be pressed, so no box. The tip
     carries the handler's description. */
  .handler {
    min-width: 0;
    max-width: 120px;
    overflow: hidden;
    color: color-mix(in srgb, var(--accent) 74%, var(--text-1));
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .handler.more {
    flex: 0 0 auto;
    color: var(--text-3);
  }
</style>
