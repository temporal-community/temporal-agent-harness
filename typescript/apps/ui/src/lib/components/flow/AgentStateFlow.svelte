<script lang="ts">
  import { untrack } from "svelte";
  import { Funnel, X } from "@lucide/svelte";
  import {
    Background,
    BackgroundVariant,
    ControlButton,
    Controls,
    MiniMap,
    SvelteFlow,
    type DefaultEdgeOptions,
    type Edge,
    type Node,
    type NodeTypes
  } from "@xyflow/svelte";
  import "@xyflow/svelte/dist/style.css";
  import MarkdownMessage from "$lib/components/chat/MarkdownMessage.svelte";
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import MetricStrip from "$lib/components/primitives/MetricStrip.svelte";
  import StateGlyph from "$lib/components/primitives/StateGlyph.svelte";
  import StatusChip, { STATUS_TONES } from "$lib/components/primitives/StatusChip.svelte";
  import { highlight } from "$lib/highlight";
  import type {
    AgentGraph,
    AgentNodeContext,
    AgentNodeData
  } from "$lib/state/flowProjection";
  import { lerpNodes, nodesMovedInPlace } from "$lib/state/nodeTween";
  import AgentStateNode, { kindFromState, setNodeOpener } from "./AgentStateNode.svelte";
  import AgentWorkflowNode from "./AgentWorkflowNode.svelte";
  import AutoFitView from "./AutoFitView.svelte";

  /* Asked per rearrangement rather than cached, so the setting takes effect
     without a reload. Absent matchMedia (the self-check paths) means no motion
     preference to honour, not a preference against motion. */
  function reduceMotion(): boolean {
    return (
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  interface Props {
    graph: AgentGraph;
    /** Snapshot paint and stream catch-up have reached their final frame. */
    ready: boolean;
    /** Settled tools leave the canvas, rather than every card being kept. */
    focus: boolean;
    onFocusChange: (focus: boolean) => void;
    onNodeSelect?: (nodeId: string) => void;
  }

  let { graph, ready, focus, onFocusChange, onNodeSelect }: Props = $props();
  let nodes = $state.raw<Node<AgentNodeData>[]>([]);
  let edges = $state.raw<Edge[]>([]);
  let settled = $state(false);
  let inspectedNode = $state<Node<AgentNodeData> | null>(null);
  let inspectorElement = $state<HTMLDialogElement | null>(null);
  let flowWrapElement = $state<HTMLDivElement | null>(null);
  let flowViewportWidth = $state(0);
  let flowViewportHeight = $state(0);
  let resizeFrame = 0;
  let tweening = $state(false);
  let tweenFrame = 0;
  /* Long enough to be followed across the pane, short enough that a reader
     toggling the view twice is not waiting on the first one. */
  const tweenMs = 260;
  const minZoom = 0.04;
  const maxZoom = 2.5;
  /* Auto-fit frames the graph but never magnifies it. A fit that runs while the
     graph is still arriving sees one node, or nodes not yet measured, and filling
     the pane with it lands at maxZoom — so every page load opened at 2.5x and then
     swept back out to the real framing. Capping the fit at 1:1 means that early
     framing is somewhere the graph can sit, not a place to escape from. Manual
     zoom is unaffected and still reaches maxZoom. */
  const fitViewOptions = { padding: 0.16, minZoom, maxZoom: 1 };
  const nodeTypes: NodeTypes = {
    agentState: AgentStateNode,
    agentWorkflow: AgentWorkflowNode
  };
  /* Edges only show the way; nothing happens on clicking one. Without the
     selection and the invisible hit path, an edge cannot turn the selection
     color and costs one path, not two. */
  const defaultEdgeOptions: DefaultEdgeOptions = { selectable: false, interactionWidth: 0 };
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

  /**
   * A rearrangement travels rather than cutting.
   *
   * Every frame rebuilds the whole projection, so folding a batch, opening one,
   * or throwing the focus switch arrives as an entirely new set of positions.
   * nodeTween decides which nodes are entitled to move; this decides when it is
   * worth doing, which is only when something that stayed also moved. Ordinary
   * streaming — text into a card that has not shifted — takes the direct
   * assignment and pays nothing.
   */
  $effect(() => {
    const next = graph.nodes;
    edges =
      ready && settled
        ? graph.edges
        : graph.edges.map((edge) => (edge.animated ? { ...edge, animated: false } : edge));
    /* untracked: this effect writes `nodes`, and reading it as a dependency
       would make every interpolated frame re-enter the effect that produced it. */
    const previous = untrack(() => nodes);

    if (tweenFrame) cancelAnimationFrame(tweenFrame);
    if (!ready || !settled || reduceMotion() || !nodesMovedInPlace(previous, next)) {
      nodes = next;
      tweening = false;
      return;
    }

    tweening = true;
    const start = performance.now();
    const step = () => {
      const t = Math.min(1, (performance.now() - start) / tweenMs);
      /* Ease out: the graph leaves briskly and arrives gently, which is what
         makes a rearrangement read as one move rather than a slide. */
      nodes = lerpNodes(previous, next, 1 - (1 - t) ** 3);
      if (t < 1) {
        tweenFrame = requestAnimationFrame(step);
        return;
      }
      tweenFrame = 0;
      tweening = false;
    };
    tweenFrame = requestAnimationFrame(step);

    return () => {
      if (tweenFrame) cancelAnimationFrame(tweenFrame);
      tweenFrame = 0;
      tweening = false;
    };
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

  function handleNodeClick(node: Node<AgentNodeData>): void {
    inspectedNode = node;
    onNodeSelect?.(node.id);
  }

  setNodeOpener((nodeId) => {
    const node = nodes.find((item) => item.id === nodeId);
    if (node) handleNodeClick(node);
  });

  function closeInspector(): void {
    inspectedNode = null;
  }

  function mapMarkClass(node: Node): string {
    const tone = (node.data as AgentNodeData | undefined)?.tone;
    if (tone === "error") return "mm-fail";
    if (tone === "queue" || tone === "approval") return "mm-wait";
    if (tone === "done") return "mm-done";
    return "mm-run";
  }

  function mapMarkColor(node: Node): string {
    const tone = (node.data as AgentNodeData | undefined)?.tone;
    if (tone === "done") return "var(--success)";
    if (tone === "error") return "var(--error)";
    if (tone === "model" || tone === "reasoning") return "var(--model)";
    if (tone === "tool") return "var(--tool)";
    if (tone === "approval") return "var(--live)";
    if (tone === "queue") return "var(--queue)";
    return "var(--accent)";
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

<div class="flow-wrap" class:settled bind:this={flowWrapElement}>
  <SvelteFlow
    bind:nodes
    bind:edges
    {nodeTypes}
    {defaultEdgeOptions}
    fitView
    {fitViewOptions}
    {minZoom}
    {maxZoom}
    colorMode="dark"
    nodesDraggable={false}
    nodesConnectable={false}
    edgesFocusable={false}
    elementsSelectable
    onnodeclick={({ node }) => handleNodeClick(node)}
    proOptions={{ hideAttribution: true }}
  >
    <AutoFitView
      signature={fitSignature}
      {fitViewOptions}
      {ready}
      hold={tweening}
      onSettled={() => (settled = true)}
    />
    <!-- No lock button: nothing on this canvas is draggable, so it only ever
         toggled selection on a pane whose single click opens an inspector.
         The tool switch takes its place rather than sitting in a bar of its own,
         because "show me less of this graph" and "show me this graph smaller"
         are the same question and belong in the same corner. -->
    <Controls {fitViewOptions} showLock={false}>
      <ControlButton
        class="graph-focus-toggle"
        aria-label="Hide finished tool calls"
        aria-pressed={focus}
        onclick={() => onFocusChange(!focus)}
      >
        <!-- Not Wrench (a tool call everywhere else in the app), and not a
             size/layout glyph (it sits under zoom and fit). -->
        <Funnel size={14} />
      </ControlButton>
    </Controls>
    <MiniMap
      pannable
      zoomable
      nodeColor={mapMarkColor}
      nodeClass={mapMarkClass}
      nodeBorderRadius={5}
      ariaLabel="Flow overview"
    />
    <Background
      variant={BackgroundVariant.Dots}
      gap={18}
      size={1}
      bgColor="var(--flow-canvas)"
      patternColor="var(--flow-dots)"
    />
  </SvelteFlow>

  {#if inspectedNode}
    {@const data = inspectedNode.data}
    {@const sections = sectionsFor(data)}
    {@const kind = kindFromState(data.state, data.statusTone ?? data.tone)}
    {@const statusTone = STATUS_TONES[kind]}
    <!-- The scrim is the dialog's own ::backdrop now, so there is no element
         between the canvas and the dialog to catch the click. A click that lands
         on the dialog itself can only have come from the backdrop, because the
         head and body fill it edge to edge. -->
    <dialog
      bind:this={inspectorElement}
      class={`node-inspector ${data.tone}`}
      style:--status-color={statusTone === "neutral" ? "var(--text-3)" : `var(--${statusTone})`}
      aria-label={`${data.title} details`}
      onclose={closeInspector}
      onclick={(event) => {
        if (event.target === inspectorElement) inspectorElement?.close();
      }}
    >
      <!-- The card's own anatomy in the pane header's row: the glyph only where
           the state warrants one, the title, and the status as a plain word. -->
      <header class="surface-head">
        <h2 class="head-name">
          <StateGlyph {kind} running={/streaming|running/.test(data.state.toLowerCase())} />
          <span class="head-title" title={data.title}>{data.title}</span>
        </h2>
        <StatusChip label={data.state} {kind} plain />
        <button
          type="button"
          class="head-button"
          aria-label="Close details"
          data-tip="Close details"
          data-tip-below
          data-tip-align="end"
          onclick={() => inspectorElement?.close()}
        >
          <X size={14} aria-hidden="true" />
        </button>
      </header>

      <!-- Scrolls as one column: the reply the node card had to clip belongs
           here in full, and it is routinely longer than the dialog is tall. -->
      <div class="inspector-body">
        {#if data.subtitle}
          <p class="inspector-subtitle" dir="auto">{data.subtitle}</p>
        {/if}

        {#if data.metrics?.length}
          <MetricStrip metrics={data.metrics} />
        {/if}

        {#each sections as section (section.label)}
          <section class="inspector-section">
            <h3 class="kicker">{section.label}</h3>
            <Copyable block value={section.text} label={`Copy ${section.label.toLowerCase()}`}>
              {#if section.kind === "markdown"}
                <div class="expanded-detail markdown" dir="auto"><MarkdownMessage text={section.text} /></div>
              {:else if section.kind === "text"}
                <pre class="expanded-detail" dir="auto">{section.text}</pre>
              {:else}
                <!-- The direction goes on the code, not the pre, so the pre keeps the
                     page's and its reserved corner stays under the copy button in RTL.
                     A tab stop, so a keyboard can scroll it where the browser does not. -->
                <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
                <pre
                  class="expanded-detail code"
                  role="region"
                  aria-label={section.label}
                  tabindex="0"
                ><code dir="ltr">{@html highlight(section.text, section.kind === "json" ? "json" : "python")}</code></pre>
              {/if}
            </Copyable>
          </section>
        {/each}
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
    background: var(--flow-canvas);
  }

  /* Keep the canvas chrome in place while Svelte Flow measures its first node
     set. Only the graph plane waits, so revealing final geometry cannot flash
     the whole pane. */
  .flow-wrap:not(.settled) :global(.svelte-flow__viewport),
  .flow-wrap:not(.settled) :global(.svelte-flow__viewport *) {
    visibility: hidden !important;
  }

  /* Centred by the UA's own :modal rule rather than by a translate, which is
     the other thing the top layer hands over for free. */
  .node-inspector {
    --tone-color: var(--text-3);
    display: flex;
    flex-direction: column;
    width: min(760px, calc(100% - var(--gutter) * 4));
    max-height: min(720px, calc(100% - var(--gutter) * 4));
    padding: 0;
    overflow: hidden;
    border: 1px solid color-mix(in srgb, var(--tone-color) 72%, var(--text-1) 6%);
    background: color-mix(in srgb, var(--tone-color) 9%, var(--surface-2));
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
  }

  .node-inspector::backdrop {
    background: var(--overlay-scrim);
  }

  /* The inset lives here, not on the dialog, so the dialog's own box is only
     ever the backdrop's click target. The dialogs' --gutter, not --pane-inset:
     it floats free of any pane edge. The column scrolls, not each section:
     several bodies of very different lengths, and a reply that is routinely
     taller than the dialog. */
  .inspector-body {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gutter);
    padding: var(--gutter);
    overflow-y: auto;
  }

  .node-inspector.neutral { --tone-color: var(--text-3); }
  .node-inspector.agent { --tone-color: var(--accent); }
  .node-inspector.model { --tone-color: var(--model); }
  .node-inspector.reasoning { --tone-color: var(--reasoning); }
  .node-inspector.tool { --tone-color: var(--tool); }
  .node-inspector.approval { --tone-color: var(--live); }
  .node-inspector.queue { --tone-color: var(--queue); }
  .node-inspector.done { --tone-color: var(--success); }
  .node-inspector.error { --tone-color: var(--error); }

  /* The status word may not shrink: one clipped character makes it ambiguous, so
     the title yields instead, as on the card. */
  .node-inspector :global(.chip) {
    flex: none;
    max-width: 40%;
  }

  /* Model ids and handler kinds are case-sensitive, so not the kicker recipe. */
  .inspector-subtitle {
    margin: 0;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
  }

  .inspector-section {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .inspector-section h3 {
    margin: 0;
  }

  .expanded-detail {
    margin: 0;
    padding: var(--gutter);
    overflow-x: auto;
    border: 1px solid color-mix(in srgb, var(--tone-color) 22%, var(--border));
    background: var(--surface-0);
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-code);
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  /* Prose reads in the body face; only its own code is monospace. */
  .expanded-detail.markdown {
    color: var(--text-1);
    font-family: inherit;
    font-size: var(--font-md);
    white-space: normal;
  }

  /* Wrapped like every block with a copy button in its corner (Copyable), so a
     long unbroken value never runs under the button or off the dialog. */
  .expanded-detail.code {
    border-color: var(--code-block-border);
    background: var(--code-block-bg);
    color: var(--code-block-text);
    box-shadow: var(--code-block-shadow);
    tab-size: 2;
  }

  .expanded-detail.code code {
    display: block;
    font: inherit;
  }

  .expanded-detail.code:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  /* Deliberately no z-index on either layer, which is what these two rules used
     to set. The library positions .svelte-flow__edges absolutely and leaves
     .svelte-flow__nodes static, so those rules were not the symmetric pair they
     read as: the nodes one was inert (a static box ignores z-index), while the
     edges one opened a stacking context that flattened every edge in the canvas
     onto a single rung at 1. Each node's own z-index then competed against that
     one number, and any node above it — an embedded subagent's boundary at 4,
     the Subagent activity container at 10 — painted over edges that were drawn,
     at the right coordinates, underneath it. Left to `auto`, both layers stay
     transparent to stacking and flowProjection's per-element z-index decides,
     which is the ordering it has always described. */

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

  /* One color per kind of flow, read by both the stroke and its arrowhead
     (flowProjection passes the variable as xyflow's marker color) so the two
     cannot drift: control into the agent in the accent, the reply out in
     green, an approval in the live color. */
  .flow-wrap {
    --edge-main: color-mix(in srgb, var(--accent) 78%, var(--border-strong));
    --edge-reasoning: color-mix(in srgb, var(--reasoning) 70%, var(--border-strong));
    --edge-approval: color-mix(in srgb, var(--live) 78%, var(--text-1) 6%);
    --edge-output: color-mix(in srgb, var(--success) 72%, var(--border-strong));
  }

  :global(.svelte-flow__edge-path) {
    stroke: var(--edge-main);
    stroke-width: 1.6;
  }

  :global(.edge-reasoning .svelte-flow__edge-path) { stroke: var(--edge-reasoning); }
  :global(.edge-approval .svelte-flow__edge-path) { stroke: var(--edge-approval); }
  :global(.edge-output .svelte-flow__edge-path) { stroke: var(--edge-output); }

  /* xyflow's `animated` edge, which flowProjection sets only on an edge into a step still
     working or waiting, redrawn as the timeline's open span: 3px on, 3px off, one 6px
     period per --dash-cycle. Only these few paths animate, and only their dash offset. */
  .flow-wrap :global(.svelte-flow__edge.animated path) {
    stroke-dasharray: 3 3;
    animation: edge-march var(--dash-cycle) linear infinite;
  }

  @keyframes edge-march {
    from { stroke-dashoffset: 6; }
  }

  @media (prefers-reduced-motion: reduce) {
    .flow-wrap :global(.svelte-flow__edge.animated path) {
      animation: none;
    }
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

  /* xyflow fills control svgs and caps them at 12px, which turns a stroked
     Lucide icon solid. Scoped to Lucide so xyflow's own filled glyphs keep
     their fill. */
  :global(.svelte-flow__controls-button svg.lucide-icon) {
    fill: none;
    max-width: none;
    max-height: none;
    width: 14px;
    height: 14px;
  }

  /* IconButton's pressed recipe; out-specifies xyflow's :hover. */
  :global(.svelte-flow__controls-button.graph-focus-toggle[aria-pressed="true"]) {
    color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 45%, transparent);
    background: color-mix(in srgb, var(--accent) 13%, var(--surface-2));
  }

  :global(.svelte-flow__minimap) {
    background: var(--surface-1);
    border: 1px solid var(--border);
  }

  /* Shape is the second channel: colour alone smears at MiniMap size.
     Diamond = failed, pill = waiting, square = done, rounded = running.
     clip-path on the SVG rect, so the MiniMap's own packing is left alone. */
  :global(.svelte-flow__minimap-node.mm-fail) {
    clip-path: polygon(50% 0, 100% 50%, 50% 100%, 0 50%);
  }

  :global(.svelte-flow__minimap-node.mm-wait) {
    clip-path: circle(50%);
  }

  :global(.svelte-flow__minimap-node.mm-done) {
    clip-path: inset(12% round 1px);
  }
</style>
