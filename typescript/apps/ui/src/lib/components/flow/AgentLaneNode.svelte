<script lang="ts">
  import { Handle, Position } from "@xyflow/svelte";
  import type { AgentNodeData } from "$lib/state/flowProjection";
  import { kindFromState } from "$lib/components/flow/AgentStateNode.svelte";
  import StatusChip from "$lib/components/primitives/StatusChip.svelte";

  interface Props {
    data: AgentNodeData;
  }

  let { data }: Props = $props();

  const statusKind = $derived(kindFromState(data.state, data.tone));
  const portTop = $derived(typeof data.portY === "number" ? `${data.portY}px` : "50%");
</script>

<!-- One message handler's share of the runtime, outlined only while several run at once.
     A delineation inside the agent's box, never a box of its own weight. -->
<div class="lane">
  <Handle id="target-left" class="lane-handle lane-handle-side" type="target" position={Position.Left} style="top: {portTop}" />
  <div class="lane-head">
    <span class="lane-label">
      <code class="lane-handler" dir="auto">{data.title}</code>
      {#if data.subtitle}
        <span class="lane-role">{data.subtitle}</span>
      {/if}
    </span>
    <span class="lane-status">
      <StatusChip label={data.state} kind={statusKind} plain />
    </span>
  </div>
  <Handle id="source-right" class="lane-handle lane-handle-side" type="source" position={Position.Right} style="top: {portTop}" />
  <Handle id="target-bottom" class="lane-handle" type="target" position={Position.Bottom} />
</div>

<style>
  /* The node box flowProjection sizes from nodeWidth/nodeHeight. */
  .lane {
    width: 100%;
    height: 100%;
    border: 1px dashed var(--lane-border);
    border-radius: var(--radius-card-inner);
    background: var(--lane-fill);
  }

  /* Held at the height flowProjection reserves (laneBox.headerHeight). */
  .lane-head {
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-md);
    padding: 0 var(--gap-md);
    color: var(--text-3);
    font-size: var(--font-xs);
  }

  .lane-label {
    min-width: 0;
    display: inline-flex;
    align-items: baseline;
    gap: var(--gap-sm);
    overflow: hidden;
    white-space: nowrap;
  }

  .lane-handler {
    overflow: hidden;
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-xs);
    text-overflow: ellipsis;
  }

  .lane-role {
    color: var(--text-3);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .lane-status {
    flex: 0 0 auto;
    font-size: var(--font-xs);
  }

  /* Zero-sized and pulled out by 1px, as the runtime boundary's are. The side ports sit at the
     lane's middle (portY), where its Input and Output cards are centred; flowProjection's
     handlesFor mirrors them, so change both together. */
  :global(.lane-handle) {
    width: 0;
    height: 0;
    min-width: 0;
    min-height: 0;
    margin: -1px;
    border: 0;
    opacity: 0;
    pointer-events: none;
  }

  :global(.lane-handle.lane-handle-side) {
    transform: none;
  }
</style>
