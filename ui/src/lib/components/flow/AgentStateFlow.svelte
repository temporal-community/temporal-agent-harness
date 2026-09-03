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
  import type {
    AgentGraph,
    AgentNodeContext,
    AgentNodeData
  } from "$lib/state/flowProjection";
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

  /**
   * The sections come from flowProjection, which is the only layer that can see
   * the frames. This used to read `data.detail` and nothing else, and print "No
   * additional context captured for this node." whenever it was unset — which is
   * every model interaction ever rendered, because nodeDataFor has never given
   * that kind a `detail`. contextFor() now guarantees a non-empty list for every
   * node kind, so there is no empty case left to write a message for.
   */
  function sectionsFor(data: AgentNodeData): AgentNodeContext[] {
    return Array.isArray(data.context) && data.context.length > 0
      ? data.context
      : [{ label: "Node", text: JSON.stringify(data, null, 2), kind: "json" }];
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
    {@const sections = sectionsFor(data)}
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

        <!-- Scrolls as one column: the reply the node card had to clip belongs
             here in full, and it is routinely longer than the dialog is tall. -->
        <div class="inspector-content">
          {#each sections as section (section.label)}
            <div class="inspector-section">
              <div class="kicker">{section.label}</div>
              <pre
                class={`expanded-detail ${section.kind}`}
                data-language={section.kind === "text" ? undefined : section.kind}
              ><code>{section.text}</code></pre>
            </div>
          {/each}
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
    display: flex;
    width: min(760px, calc(100% - var(--gutter) * 4));
    max-height: min(720px, calc(100% - var(--gutter) * 4));
    padding: 0;
    overflow: hidden;
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
    flex: 1;
    min-width: 0;
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

  /* The column scrolls, not each section: several bodies of very different
     lengths, and a reply that is routinely taller than the dialog. */
  .inspector-content {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gutter);
    overflow-y: auto;
  }

  .inspector-section {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .expanded-detail {
    position: relative;
    margin: 0;
    padding: var(--gutter);
    overflow-x: auto;
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

  /* Puts back the baseline ring app.css gives every other focusable thing. A
     node was the one focusable element on the page with none: the library's own
     stylesheet sets `outline: none` on .svelte-flow__node.selectable:focus-visible,
     so tabbing the canvas moved an invisible cursor. Nested under .flow-wrap
     rather than written flat, because that rule is three classes deep and a
     flat :global() would lose to it — the scoped ancestor is how this file
     outranks a vendor stylesheet without a specificity hack.

     Offset outward, because a flush outline is what selection draws: a keyboard
     user who has focused a node without selecting it must not be shown the
     selected mark, and a node that is both wears the two rings concentrically. */
  .flow-wrap :global(.svelte-flow__node:focus-visible) {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
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
