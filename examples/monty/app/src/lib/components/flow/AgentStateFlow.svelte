<script lang="ts">
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

  $effect(() => {
    nodes = graph.nodes;
    edges = graph.edges;
  });
</script>

<div class="flow-wrap">
  <SvelteFlow
    bind:nodes
    bind:edges
    {nodeTypes}
    fitView
    fitViewOptions={{ padding: 0.12 }}
    colorMode="dark"
    nodesDraggable={false}
    nodesConnectable={false}
    elementsSelectable
    onnodeclick={({ node }) => onNodeSelect?.(node.id)}
    proOptions={{ hideAttribution: true }}
  >
    <AutoFitView signature={autoFitSignature} />
    <Controls />
    <Background variant={BackgroundVariant.Dots} gap={18} size={1} />
  </SvelteFlow>
</div>

<style>
  .flow-wrap {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 0;
    background: var(--surface-0);
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
