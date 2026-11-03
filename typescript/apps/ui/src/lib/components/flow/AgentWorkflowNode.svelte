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
  <!-- The raised floor the runtime's cards stand on (they are separate nodes, laid over it). -->
  <div class="workflow-body"></div>
  <Handle id="source-bottom" class="workflow-handle" type="source" position={Position.Bottom} />
</div>

<style>
  /* Rounded like the cards it holds (see --radius-card in app.css), and coloured the
     same way: the runtime's tone tints the head and the hairline, over IO's neutral
     surfaces. No lift — it is the ground the cards stand on. */
  .workflow-boundary {
    --status-color: var(--accent);
    --frame-border: color-mix(in srgb, var(--status-color) 34%, var(--color-border-primary));
    /* Takes the pointer, so hovering a subagent's frame is not hovering the Subagent activity
       card under it: that card's footer would come up over the frame, as if it were the frame's. */
    pointer-events: auto;
    display: flex;
    flex-direction: column;
    border: 1px solid var(--frame-border);
    border-radius: var(--radius-card);
    /* No more than 8%: --text-3 on a 12% green band measured 4.36:1. */
    background: color-mix(in srgb, var(--status-color) 8%, var(--surface-1));
  }

  /* A subagent's frame sits on a card's body rather than the canvas, so it takes a
     little less tint to read as one step further in. */
  .workflow-boundary.subagent {
    background: color-mix(in srgb, var(--status-color) 6%, var(--surface-1));
  }

  /* The raised floor, flush with the frame's sides and foot like a card's body. */
  .workflow-body {
    flex: 1 1 auto;
    min-height: 0;
    margin: 0 -1px -1px;
    border: 1px solid var(--frame-border);
    border-radius: var(--radius-card-inner);
    background: var(--color-surface-primary);
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

  /* Held at the height flowProjection reserves (runtimeHeaderHeight): the runtime's
     cards are laid out from that number, not from this box. */
  .workflow-head {
    flex: none;
    min-height: 88px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--gap-lg);
    padding: var(--gap-lg) var(--gap-lg) var(--gap-md);
    color: var(--color-content-primary);
    font-size: var(--font-md);
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
    font-size: var(--font-xl);
    font-weight: 500;
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
    color: var(--color-content-secondary);
    font-size: var(--font-md);
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
    max-width: 96px;
    overflow: hidden;
    padding: var(--gap-2xs) var(--gap-md);
    border: 1px solid color-mix(in srgb, var(--accent) 34%, var(--color-border-primary));
    border-radius: var(--radius-pill);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    color: color-mix(in srgb, var(--accent) 74%, var(--color-content-primary));
    font-family: var(--font-sans);
    font-size: var(--font-xs);
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .handler.more {
    flex: 0 0 auto;
    max-width: none;
    border-color: var(--color-border-primary);
    background: var(--color-surface-overlay-secondary);
    color: var(--color-content-tertiary);
  }
</style>
