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
  import Badge from "$lib/components/primitives/Badge.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import MetricStrip from "$lib/components/primitives/MetricStrip.svelte";
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
  let inspectorElement = $state<HTMLDialogElement | null>(null);
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

  /* showModal() rather than the `open` attribute, which is what this used to
     set. The top layer is what grants a dialog its focus trap, its Escape, its
     focus restore, and a real ::backdrop; with `open` alone the browser gives
     none of them, so Escape and the scrim were hand-rolled and the focus
     behaviour was simply missing. */
  $effect(() => {
    const element = inspectorElement;
    if (element && !element.open) element.showModal();
  });
</script>

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
    <!-- The scrim is the dialog's own ::backdrop now, so there is no element
         between the canvas and the dialog to catch the click. A click that lands
         on the dialog itself can only have come from the backdrop, because the
         body fills it edge to edge. -->
    <dialog
      bind:this={inspectorElement}
      class={`node-inspector ${data.tone}`}
      aria-label={`${data.title} context`}
      onclose={closeInspector}
      onclick={(event) => {
        if (event.target === inspectorElement) inspectorElement?.close();
      }}
    >
      <div class="inspector-body">
        <header class="inspector-header">
          <div class="inspector-heading">
            <span class={`inspector-dot ${data.dotTone ?? data.tone}`} aria-hidden="true"></span>
            <div>
              <h2>{data.title}</h2>
              <p>{data.subtitle ?? "state diagram node"}</p>
            </div>
          </div>
          <div class="inspector-actions">
            <Badge label={data.state} tone={data.statusTone ?? data.tone} size="sm" />
            <IconButton label="Close node context" onclick={() => inspectorElement?.close()}>
              <X size={16} />
            </IconButton>
          </div>
        </header>

        {#if data.metrics?.length}
          <MetricStrip metrics={data.metrics} />
        {/if}

        <div class="inspector-content">
          <div class="kicker">{detail.label}</div>
          <pre
            class={`expanded-detail ${detail.kind}`}
            data-language={detail.kind === "json" ? "json" : detail.kind === "code" ? "code" : undefined}
          ><code>{detail.text}</code></pre>
        </div>
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

  /* Centred by the UA's own :modal rule rather than by a translate, which is
     the other thing the top layer hands over for free. */
  .node-inspector {
    --tone-color: var(--text-3);
    width: min(760px, calc(100% - var(--gutter) * 4));
    max-height: min(720px, calc(100% - var(--gutter) * 4));
    padding: 0;
    border: 1px solid color-mix(in srgb, var(--tone-color) 72%, white 6%);
    background: color-mix(in srgb, var(--tone-color) 9%, var(--surface-2));
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
  }

  .node-inspector::backdrop {
    background: var(--overlay-scrim);
  }

  /* The inset lives here, not on the dialog, so the dialog's own box is only
     ever the backdrop's click target. */
  .inspector-body {
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gutter);
    padding: var(--gutter);
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
    gap: var(--gutter);
    min-width: 0;
  }

  .inspector-heading {
    display: flex;
    align-items: flex-start;
    gap: var(--gutter-tight);
    min-width: 0;
  }

  .inspector-heading h2 {
    margin: 0;
    font-size: var(--font-title);
    line-height: 1.2;
    letter-spacing: 0;
  }

  .inspector-heading p {
    margin: var(--gap-xs) 0 0;
    color: var(--text-2);
    font-size: var(--font-lg);
    line-height: 1.35;
    word-break: break-word;
  }

  /* The one part of the header with no primitive behind it: a tone square, the
     same mark Chip draws as its pip and the node card draws beside its title.
     Sized by the pip token so all three stay one size, and unringed because the
     elevation layer has no blooms in it. */
  .inspector-dot {
    flex: 0 0 auto;
    width: var(--pip-lg);
    height: var(--pip-lg);
    margin-top: var(--gap-md);
    background: var(--tone-color);
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
    gap: var(--gap-md);
    flex: 0 0 auto;
  }

  .inspector-content {
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .expanded-detail {
    position: relative;
    min-height: 180px;
    max-height: min(520px, calc(100vh - 260px));
    margin: 0;
    padding: var(--gutter);
    overflow: auto;
    border: 1px solid color-mix(in srgb, var(--tone-color) 22%, var(--border));
    background: color-mix(in srgb, var(--surface-0) 86%, black 10%);
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-code);
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .expanded-detail.code,
  .expanded-detail.json {
    /* The gutter, plus a chip row for the language tag to sit in. */
    padding-top: calc(var(--gutter) + var(--control-height-xs));
    border-color: var(--code-block-border);
    background: var(--code-block-bg);
    color: var(--code-block-text);
    box-shadow: var(--code-block-shadow);
    white-space: pre;
    overflow-wrap: normal;
  }

  /* app.css draws the language tag; this only says where to put it. */
  .expanded-detail.code::before,
  .expanded-detail.json::before {
    position: absolute;
    top: var(--gap-md);
    right: var(--gap-md);
  }

  :global(.svelte-flow__edges) {
    z-index: 1;
  }

  :global(.svelte-flow__nodes) {
    z-index: 2;
  }

  :global(.svelte-flow__edge-text) {
    fill: var(--text-2);
    font-size: var(--font-sm);
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
