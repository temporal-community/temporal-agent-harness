<script lang="ts">
  import { X } from "@lucide/svelte";
  import {
    Background,
    BackgroundVariant,
    Controls,
    MiniMap,
    SvelteFlow,
    type Edge,
    type Node,
    type NodeTypes
  } from "@xyflow/svelte";
  import "@xyflow/svelte/dist/style.css";
  import type { AgentGraph, AgentNodeData } from "$lib/state/flowProjection";
  import AgentStateNode from "./AgentStateNode.svelte";
  import AgentWorkflowNode from "./AgentWorkflowNode.svelte";
  import AutoFitView from "./AutoFitView.svelte";

  interface Props {
    graph: AgentGraph;
    onNodeSelect?: (nodeId: string) => void;
  }

  let { graph, onNodeSelect }: Props = $props();
  let nodes = $state.raw<Node<AgentNodeData>[]>([]);
  let edges = $state.raw<Edge[]>([]);
  let inspectedNode = $state<Node<AgentNodeData> | null>(null);
  let flowWrapElement = $state<HTMLDivElement | null>(null);
  let flowViewportWidth = $state(0);
  let flowViewportHeight = $state(0);
  let resizeFrame = 0;
  const minZoom = 0.04;
  const maxZoom = 2.5;
  const fitViewOptions = { padding: 0.16, minZoom, maxZoom };
  const nodeTypes: NodeTypes = {
    agentState: AgentStateNode,
    agentWorkflow: AgentWorkflowNode
  };
  const autoFitSignature = $derived(
    graph.nodes
      .map((item) =>
        [
          item.id,
          item.type ?? "default",
          item.position.x,
          item.position.y,
          item.data.size ?? "default",
          typeof item.data.nodeWidth === "number" ? item.data.nodeWidth : "",
          typeof item.data.nodeHeight === "number" ? item.data.nodeHeight : "",
          typeof item.data.boundaryWidth === "number" ? item.data.boundaryWidth : "",
          typeof item.data.boundaryHeight === "number" ? item.data.boundaryHeight : ""
        ].join(":")
      )
      .join("|")
  );
  const viewportSignature = $derived(`${flowViewportWidth}x${flowViewportHeight}`);
  const fitSignature = $derived(`${autoFitSignature}|${viewportSignature}`);

  $effect(() => {
    nodes = graph.nodes;
    edges = graph.edges;
  });

  $effect(() => {
    const element = flowWrapElement;
    if (!element || typeof ResizeObserver === "undefined") return;

    const updateSize = (width: number, height: number) => {
      if (resizeFrame) cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => {
        flowViewportWidth = Math.round(width);
        flowViewportHeight = Math.round(height);
        resizeFrame = 0;
      });
    };

    updateSize(element.clientWidth, element.clientHeight);
    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      updateSize(entry.contentRect.width, entry.contentRect.height);
    });
    observer.observe(element);

    return () => {
      observer.disconnect();
      if (resizeFrame) cancelAnimationFrame(resizeFrame);
      resizeFrame = 0;
    };
  });

  function detailText(data: AgentNodeData): { label: string; text: string; kind: "code" | "json" | "text" } {
    const detail = data.detail;
    if (typeof detail !== "string" || !detail.trim()) {
      return { label: "Context", text: "No additional context captured for this node.", kind: "text" };
    }

    try {
      const parsed = JSON.parse(detail);
      const script =
        typeof parsed?.script === "string"
          ? parsed.script
          : typeof parsed?.payload?.script === "string"
            ? parsed.payload.script
            : null;
      if (script) return { label: "Script", text: script, kind: "code" };
      return { label: "Context", text: JSON.stringify(parsed, null, 2), kind: "json" };
    } catch {
      return { label: "Context", text: detail, kind: looksLikeCode(detail) ? "code" : "text" };
    }
  }

  function looksLikeCode(value: string): boolean {
    return /^\s*(def |class |import |from |async def |value\s*=|answer\s*=|\{)/m.test(value);
  }

  function inspectNode(node: Node<AgentNodeData>): void {
    inspectedNode = node;
    onNodeSelect?.(node.id);
  }

  function closeInspector(): void {
    inspectedNode = null;
  }

  function handleInspectorKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") closeInspector();
  }
</script>

<svelte:window onkeydown={handleInspectorKeydown} />

<div class="flow-wrap" bind:this={flowWrapElement}>
  <SvelteFlow
    bind:nodes
    bind:edges
    {nodeTypes}
    fitView
    {fitViewOptions}
    {minZoom}
    {maxZoom}
    colorMode="dark"
    nodesDraggable={false}
    nodesConnectable={false}
    elementsSelectable
    onnodeclick={({ node }) => inspectNode(node)}
    proOptions={{ hideAttribution: true }}
  >
    <AutoFitView signature={fitSignature} {fitViewOptions} />
    <Controls {fitViewOptions} />
    <Background variant={BackgroundVariant.Dots} gap={18} size={1} />
  </SvelteFlow>

  {#if inspectedNode}
    {@const data = inspectedNode.data}
    {@const detail = detailText(data)}
    <button
      type="button"
      class="inspector-backdrop"
      aria-label="Close node context"
      onclick={closeInspector}
    ></button>
    <dialog
      class={`node-inspector ${data.tone}`}
      aria-label={`${data.title} context`}
      open
    >
      <header class="inspector-header">
        <div class="inspector-heading">
          <span class={`inspector-dot ${data.dotTone ?? data.tone}`} aria-hidden="true"></span>
          <div>
            <h2>{data.title}</h2>
            <p>{data.subtitle ?? "state diagram node"}</p>
          </div>
        </div>
        <div class="inspector-actions">
          <span class="inspector-state">{data.state}</span>
          <button type="button" class="close-button" aria-label="Close node context" onclick={closeInspector}>
            <X size={18} />
          </button>
        </div>
      </header>

      {#if data.metrics?.length}
        <div class="inspector-metrics">
          {#each data.metrics as metric}
            <span><strong>{metric.value}</strong>{metric.label}</span>
          {/each}
        </div>
      {/if}

      <div class="inspector-content">
        <div class="content-label">{detail.label}</div>
        <pre class={`expanded-detail ${detail.kind}`}><code>{detail.text}</code></pre>
      </div>
    </dialog>
  {/if}
</div>

<style>
  .flow-wrap {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 0;
    background: var(--surface-0);
  }

  .inspector-backdrop {
    position: absolute;
    inset: 0;
    z-index: 20;
    border: 0;
    background: rgba(0, 0, 0, 0.42);
    cursor: default;
  }

  .node-inspector {
    --tone-color: var(--text-3);
    position: absolute;
    z-index: 21;
    left: 50%;
    top: 50%;
    width: min(760px, calc(100% - 48px));
    max-height: min(720px, calc(100% - 48px));
    transform: translate(-50%, -50%);
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 20px;
    border: 2px solid color-mix(in srgb, var(--tone-color) 72%, white 6%);
    border-radius: 12px;
    background: color-mix(in srgb, var(--tone-color) 9%, var(--surface-2));
    color: var(--text-1);
    box-shadow:
      0 0 0 1px color-mix(in srgb, var(--tone-color) 26%, transparent),
      0 26px 80px rgba(0, 0, 0, 0.62);
  }

  .node-inspector.neutral { --tone-color: var(--text-3); }
  .node-inspector.agent { --tone-color: var(--accent); }
  .node-inspector.model { --tone-color: var(--model); }
  .node-inspector.reasoning { --tone-color: var(--reasoning); }
  .node-inspector.tool { --tone-color: var(--warning); }
  .node-inspector.approval,
  .node-inspector.queue { --tone-color: var(--queue); }
  .node-inspector.done { --tone-color: var(--success); }
  .node-inspector.error { --tone-color: var(--error); }

  .inspector-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    min-width: 0;
  }

  .inspector-heading {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    min-width: 0;
  }

  .inspector-heading h2 {
    margin: 0;
    font-size: 22px;
    line-height: 1.2;
    letter-spacing: 0;
  }

  .inspector-heading p {
    margin: 5px 0 0;
    color: var(--text-2);
    font-size: 13px;
    line-height: 1.35;
    word-break: break-word;
  }

  .inspector-dot {
    flex: 0 0 auto;
    width: 11px;
    height: 11px;
    margin-top: 8px;
    border-radius: 999px;
    background: var(--tone-color);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--tone-color) 18%, transparent);
  }

  .inspector-dot.neutral { background: var(--text-3); }
  .inspector-dot.agent { background: var(--accent); }
  .inspector-dot.model { background: var(--model); }
  .inspector-dot.reasoning { background: var(--reasoning); }
  .inspector-dot.tool { background: var(--warning); }
  .inspector-dot.approval,
  .inspector-dot.queue { background: var(--queue); }
  .inspector-dot.done { background: var(--success); }
  .inspector-dot.error { background: var(--error); }

  .inspector-actions {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 0 0 auto;
  }

  .inspector-state {
    max-width: 190px;
    padding: 5px 10px;
    border: 1px solid color-mix(in srgb, var(--tone-color) 34%, var(--border));
    border-radius: 999px;
    color: var(--text-2);
    background: color-mix(in srgb, var(--tone-color) 10%, var(--surface-1));
    font-size: 12px;
    font-weight: 700;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .close-button {
    display: inline-grid;
    place-items: center;
    width: 34px;
    height: 34px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: color-mix(in srgb, var(--surface-3) 72%, transparent);
    color: var(--text-2);
    cursor: pointer;
  }

  .close-button:hover {
    color: var(--text-1);
    border-color: var(--border-strong);
  }

  .inspector-metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }

  .inspector-metrics span {
    min-width: 84px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text-3);
    background: color-mix(in srgb, var(--surface-1) 84%, transparent);
    font-size: 10px;
    text-transform: uppercase;
  }

  .inspector-metrics strong {
    display: block;
    margin-bottom: 2px;
    color: var(--text-1);
    font-size: 13px;
    font-variant-numeric: tabular-nums;
    text-transform: none;
  }

  .inspector-content {
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 7px;
  }

  .content-label {
    color: var(--text-3);
    font-size: 11px;
    font-weight: 750;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .expanded-detail {
    min-height: 180px;
    max-height: min(520px, calc(100vh - 260px));
    margin: 0;
    padding: 16px;
    overflow: auto;
    border: 1px solid color-mix(in srgb, var(--tone-color) 22%, var(--border));
    border-radius: 8px;
    background: color-mix(in srgb, var(--surface-0) 86%, black 10%);
    color: var(--text-2);
    font-family:
      ui-monospace,
      SFMono-Regular,
      Menlo,
      Monaco,
      Consolas,
      "Liberation Mono",
      monospace;
    font-size: 13px;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .expanded-detail.code,
  .expanded-detail.json {
    white-space: pre;
    overflow-wrap: normal;
  }

  :global(.svelte-flow__edges) {
    z-index: 1;
  }

  :global(.svelte-flow__nodes) {
    z-index: 2;
  }

  :global(.svelte-flow__edge-text) {
    fill: var(--text-2);
    font-size: 11px;
    font-weight: 650;
  }

  :global(.svelte-flow__edge-path) {
    stroke: color-mix(in srgb, var(--accent) 45%, var(--border-strong));
    stroke-width: 1.4;
  }

  :global(.edge-main .svelte-flow__edge-path) {
    stroke: color-mix(in srgb, var(--accent) 48%, var(--border-strong));
  }

  :global(.edge-reasoning .svelte-flow__edge-path) {
    stroke: color-mix(in srgb, var(--reasoning) 62%, var(--border-strong));
    stroke-width: 1.3;
  }

  :global(.edge-approval .svelte-flow__edge-path) {
    stroke: color-mix(in srgb, var(--queue) 78%, white 6%);
    stroke-width: 2;
    stroke-dasharray: 6 5;
  }

  :global(.edge-approval .svelte-flow__edge-text) {
    fill: color-mix(in srgb, var(--queue) 82%, white 8%);
  }

  :global(.edge-output .svelte-flow__edge-path) {
    stroke: color-mix(in srgb, var(--success) 58%, var(--border-strong));
  }

  :global(.svelte-flow__controls) {
    border: 1px solid var(--border);
    box-shadow: none;
  }

  :global(.svelte-flow__controls-button) {
    background: var(--surface-2);
    border-bottom-color: var(--border);
    color: var(--text-2);
  }

  :global(.svelte-flow__minimap) {
    background: var(--surface-1);
    border: 1px solid var(--border);
  }
</style>
