<script lang="ts">
  import { onDestroy } from "svelte";
  import type { Attachment } from "svelte/attachments";
  import { prefersReducedMotion } from "svelte/motion";
  import {
    forceCollide,
    forceLink,
    forceManyBody,
    forceSimulation,
    forceX,
    forceY,
    type Simulation,
    type SimulationLinkDatum,
    type SimulationNodeDatum
  } from "d3-force";
  import type { OKFGraph } from "$lib/api/types";
  import {
    colorOf,
    graphElements,
    graphShape,
    matches,
    seededRandom,
    type GraphEdge,
    type GraphNode
  } from "$lib/state/okfGraph";

  /**
   * An OKF bundle as a force-directed graph: concepts as nodes colored by type, links as
   * edges, and a ghost node for each link to a concept that doesn't exist yet.
   *
   * The simulation runs outside Svelte's reactive state: it moves plain node objects, and
   * each tick schedules one frame that writes the new positions straight onto the elements
   * already drawn, a transform per node and one path per look of edge. Svelte redraws only when what is drawn changes — the graph, a filter,
   * the selection — never per tick: re-rendering 10,000 nodes and their edges through it
   * on every tick was most of the frame. Positions are kept by concept id, so when the
   * bundle grows the new concepts settle in beside the ones already placed, and nothing
   * jumps. Before the simulation starts (and in a server render) nodes sit on a fixed
   * spiral, so a given bundle always starts the same way.
   */
  interface Props {
    graph: OKFGraph;
    colors: Map<string, string>;
    selectedId?: string | null;
    /** Nodes that don't match are dimmed. */
    query?: string;
    /** Types whose concepts are hidden, with their links. */
    hiddenTypes?: string[];
  }

  let {
    graph,
    colors,
    selectedId = $bindable(null),
    query = "",
    hiddenTypes = []
  }: Props = $props();

  interface SimNode extends SimulationNodeDatum {
    id: string;
    node: GraphNode;
  }
  type SimLink = SimulationLinkDatum<SimNode> & { edge: GraphEdge };

  const WIDTH = 800;
  const HEIGHT = 560;

  // Not reactive: the simulation mutates these in place, and `draw` copies them to the DOM.
  const placed = new Map<string, SimNode>();
  let simulation: Simulation<SimNode, SimLink> | null = null;
  /** The node and edge ids the simulation was last started on. */
  let shape = "";

  const elements = $derived(graphElements(graph));

  /** Where a node starts: beside a linked node already placed, else on the spiral. */
  function start(id: string, index: number, edges: GraphEdge[]): { x: number; y: number } {
    for (const edge of edges) {
      const other = edge.source === id ? edge.target : edge.target === id ? edge.source : null;
      const near = other ? placed.get(other) : undefined;
      if (near?.x !== undefined && near.y !== undefined) {
        const angle = index * 2.399963;
        return { x: near.x + 30 * Math.cos(angle), y: near.y + 30 * Math.sin(angle) };
      }
    }
    const r = 26 * Math.sqrt(index + 0.5);
    const angle = index * 2.399963;
    return { x: r * Math.cos(angle), y: r * Math.sin(angle) };
  }

  const simNodes = $derived.by(() => {
    const { nodes, edges } = elements;
    const live = new Set(nodes.map((n) => n.id));
    for (const id of [...placed.keys()]) if (!live.has(id)) placed.delete(id);
    return nodes.map((node, index) => {
      const existing = placed.get(node.id);
      if (existing) {
        existing.node = node;
        return existing;
      }
      const created: SimNode = { id: node.id, node, ...start(node.id, index, edges) };
      placed.set(node.id, created);
      return created;
    });
  });

  $effect(() => {
    const nodes = simNodes;
    const links: SimLink[] = elements.edges.map((edge) => ({
      source: edge.source,
      target: edge.target,
      edge
    }));
    if (!simulation) {
      simulation = forceSimulation<SimNode, SimLink>()
        .randomSource(seededRandom(7))
        .force("charge", forceManyBody<SimNode>().strength(-340).distanceMax(480))
        .force("collide", forceCollide<SimNode>((d) => d.node.radius + 14))
        .force("x", forceX<SimNode>(0).strength(0.045))
        .force("y", forceY<SimNode>(0).strength(0.06))
        .on("tick", scheduleDraw);
    }
    simulation.nodes(nodes);
    simulation.force(
      "link",
      forceLink<SimNode, SimLink>(links)
        .id((d) => d.id)
        .distance(105)
        .strength(0.35)
    );
    /* A re-walk that found the same concepts and links is new data, not a new layout:
       restarting would shake a settled graph every time its mount's state moved. d3
       stops the timer by itself once alpha falls under alphaMin. */
    const next = graphShape(elements);
    if (next === shape) return;
    shape = next;
    // Warm restart: what was already placed barely moves; new nodes settle in.
    simulation.alpha(Math.max(simulation.alpha(), 0.35));
    /* Settled off screen and drawn once, where the layout must not move by itself. 300 ticks
       is d3's own run from alpha 1 down to alphaMin. */
    if (prefersReducedMotion.current) {
      simulation.stop().tick(300);
      scheduleDraw();
    } else simulation.restart();
  });

  onDestroy(() => {
    simulation?.stop();
    /* Guarded because a server render runs onDestroy too, and has no frames to cancel. */
    if (pending !== 0) cancelAnimationFrame(pending);
  });

  const hidden = $derived(new Set(hiddenTypes));

  /** What is drawn, with the simulation's nodes to read positions from; not per tick. */
  const drawn = $derived.by(() => {
    const at = new Map(simNodes.map((n) => [n.id, n]));
    const visible = simNodes.filter((n) => !hidden.has(n.node.type));
    const shown = new Set(visible.map((n) => n.id));
    const focus = selectedId;
    const neighbors = new Set<string>();
    for (const edge of elements.edges) {
      if (edge.source === focus) neighbors.add(edge.target);
      if (edge.target === focus) neighbors.add(edge.source);
    }
    return {
      nodes: visible.map((n) => ({
        ...n.node,
        sim: n,
        dim: !matches(n.node, query),
        selected: n.id === focus,
        neighbor: neighbors.has(n.id)
      })),
      edges: elements.edges
        .filter((e) => shown.has(e.source) && shown.has(e.target))
        .map((e) => ({
          ...e,
          from: at.get(e.source)!,
          to: at.get(e.target)!,
          lit: e.source === focus || e.target === focus
        }))
    };
  });

  type DrawnEdge = (typeof drawn.edges)[number];

  function translate(node: SimNode): string {
    return `translate(${node.x ?? 0} ${node.y ?? 0})`;
  }

  /* Edges are drawn as one path per look rather than a line each: a tick is then two writes
     per look instead of four per edge, which at 4,000 edges was most of the draw. Lit last,
     so the selection's edges sit on top. */
  const EDGE_LOOKS = ["plain", "dangling", "lit"] as const;
  type EdgeLook = (typeof EDGE_LOOKS)[number];

  /* The arrowhead's length and half-width, in graph units. */
  const ARROW = 7;
  const ARROW_HALF = 3.5;

  const round = (value: number) => Math.round(value * 10) / 10;

  /**
   * Every edge as path data, by look: a segment from its source's center to just short of
   * its target's rim, and a filled arrowhead with its tip there.
   */
  function edgePaths(edges: DrawnEdge[]): Record<EdgeLook, { line: string; arrow: string }> {
    const paths = {
      plain: { line: "", arrow: "" },
      dangling: { line: "", arrow: "" },
      lit: { line: "", arrow: "" }
    };
    for (const edge of edges) {
      const x1 = edge.from.x ?? 0;
      const y1 = edge.from.y ?? 0;
      const dx = (edge.to.x ?? 0) - x1;
      const dy = (edge.to.y ?? 0) - y1;
      const length = Math.hypot(dx, dy) || 1;
      const ux = dx / length;
      const uy = dy / length;
      const inset = edge.to.node.radius + 3;
      const x2 = x1 + dx - ux * inset;
      const y2 = y1 + dy - uy * inset;
      const bx = x2 - ux * ARROW;
      const by = y2 - uy * ARROW;
      const path = paths[edge.lit ? "lit" : edge.dangling ? "dangling" : "plain"];
      path.line += `M${round(x1)} ${round(y1)}L${round(x2)} ${round(y2)}`;
      path.arrow +=
        `M${round(x2)} ${round(y2)}L${round(bx - uy * ARROW_HALF)} ${round(by + ux * ARROW_HALF)}` +
        `L${round(bx + uy * ARROW_HALF)} ${round(by - ux * ARROW_HALF)}Z`;
    }
    return paths;
  }

  const edgeData = $derived(edgePaths(drawn.edges));

  /* The elements drawn for each node and each look of edge, so a tick can move them without Svelte. */
  const nodeElements = new Map<string, Element>();
  const edgeElements = new Map<string, Element>();

  function keep(elements: Map<string, Element>, id: string): Attachment<Element> {
    return (element) => {
      elements.set(id, element);
      return () => {
        if (elements.get(id) === element) elements.delete(id);
      };
    };
  }

  let pending = 0;

  /* One write per frame however many ticks land in it. */
  function scheduleDraw(): void {
    if (pending === 0) pending = requestAnimationFrame(draw);
  }

  function draw(): void {
    pending = 0;
    for (const node of drawn.nodes) nodeElements.get(node.id)?.setAttribute("transform", translate(node.sim));
    const paths = edgePaths(drawn.edges);
    for (const look of EDGE_LOOKS) {
      edgeElements.get(`${look}:line`)?.setAttribute("d", paths[look].line);
      edgeElements.get(`${look}:arrow`)?.setAttribute("d", paths[look].arrow);
    }
  }

  // ---------------------------------------------------------------- pan, zoom, drag

  let svg = $state<SVGSVGElement | null>(null);
  let layer = $state<SVGGElement | null>(null);
  let view = $state({ x: 0, y: 0, k: 1 });
  let gesture:
    | { kind: "pan"; startX: number; startY: number; viewX: number; viewY: number }
    | { kind: "drag"; node: SimNode; startX: number; startY: number; moved: boolean }
    | null = null;

  /** The pointer, in the graph's own coordinates. */
  function graphPoint(event: PointerEvent): { x: number; y: number } | null {
    const matrix = layer?.getScreenCTM();
    if (!svg || !matrix) return null;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const at = point.matrixTransform(matrix.inverse());
    return { x: at.x, y: at.y };
  }

  /** How many graph units one screen pixel is, before zoom. */
  function unitsPerPixel(): number {
    const rect = svg?.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return 1;
    return Math.max(WIDTH / rect.width, HEIGHT / rect.height);
  }

  function onBackgroundDown(event: PointerEvent) {
    if (event.button !== 0) return;
    (event.currentTarget as Element).setPointerCapture(event.pointerId);
    gesture = {
      kind: "pan",
      startX: event.clientX,
      startY: event.clientY,
      viewX: view.x,
      viewY: view.y
    };
  }

  function onNodeDown(event: PointerEvent, id: string) {
    if (event.button !== 0) return;
    event.stopPropagation();
    const node = placed.get(id);
    if (!node) return;
    svg?.setPointerCapture(event.pointerId);
    gesture = { kind: "drag", node, startX: event.clientX, startY: event.clientY, moved: false };
  }

  function onMove(event: PointerEvent) {
    if (!gesture) return;
    if (gesture.kind === "pan") {
      const scale = unitsPerPixel();
      view = {
        ...view,
        x: gesture.viewX + (event.clientX - gesture.startX) * scale,
        y: gesture.viewY + (event.clientY - gesture.startY) * scale
      };
      return;
    }
    if (!gesture.moved && Math.hypot(event.clientX - gesture.startX, event.clientY - gesture.startY) < 3)
      return;
    gesture.moved = true;
    const at = graphPoint(event);
    if (!at) return;
    gesture.node.fx = at.x;
    gesture.node.fy = at.y;
    simulation?.alphaTarget(0.25).restart();
  }

  function onUp() {
    if (gesture?.kind === "drag") {
      if (!gesture.moved) selectedId = gesture.node.id;
      gesture.node.fx = null;
      gesture.node.fy = null;
      simulation?.alphaTarget(0);
    }
    gesture = null;
  }

  function onWheel(event: WheelEvent) {
    event.preventDefault();
    const k = Math.min(4, Math.max(0.25, view.k * Math.exp(-event.deltaY * 0.0015)));
    view = { ...view, k };
  }

  function shortLabel(label: string): string {
    return label.length > 26 ? `${label.slice(0, 25)}…` : label;
  }
</script>

<svg
  bind:this={svg}
  class="graph"
  viewBox={`${-WIDTH / 2} ${-HEIGHT / 2} ${WIDTH} ${HEIGHT}`}
  role="application"
  aria-label="Knowledge graph"
  onpointerdown={onBackgroundDown}
  onpointermove={onMove}
  onpointerup={onUp}
  onpointercancel={onUp}
  onwheel={onWheel}
>
  <g bind:this={layer} transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
    {#each EDGE_LOOKS as look (look)}
      <path class={`edges ${look}`} d={edgeData[look].line} {@attach keep(edgeElements, `${look}:line`)} />
      <path class={`arrows ${look}`} d={edgeData[look].arrow} {@attach keep(edgeElements, `${look}:arrow`)} />
    {/each}
    {#each drawn.nodes as node (node.id)}
      <g
        class="node"
        class:ghost={node.ghost}
        class:dim={node.dim}
        class:selected={node.selected}
        class:neighbor={node.neighbor}
        class:deprecated={node.concept?.status === "deprecated"}
        class:draft={node.concept?.status === "draft"}
        data-concept={node.id}
        transform={translate(node.sim)}
        {@attach keep(nodeElements, node.id)}
        role="button"
        tabindex="0"
        aria-label={`${node.label} (${node.ghost ? "not written yet" : node.type})`}
        aria-pressed={node.selected}
        onpointerdown={(event) => onNodeDown(event, node.id)}
        onkeydown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectedId = node.id;
          }
        }}
      >
        <circle
          class="dot"
          r={node.radius}
          style={`--node: ${node.ghost ? "var(--text-4)" : colorOf(colors, node.type)}`}
        />
        {#if node.concept?.problem}
          <circle class="mark problem" r={node.radius + 4} />
        {/if}
        {#if node.concept?.stale}
          <circle class="badge stale" cx={node.radius * 0.75} cy={-node.radius * 0.75} r="3.5">
            <title>Stale</title>
          </circle>
        {/if}
        {#if node.concept?.trust_tier === "human-reviewed"}
          <circle class="badge verified" cx={-node.radius * 0.75} cy={-node.radius * 0.75} r="3.5">
            <title>Confirmed by a person</title>
          </circle>
        {/if}
        <text class="label" y={node.radius + 13}>{shortLabel(node.label)}</text>
      </g>
    {/each}
  </g>
</svg>

<style>
  .graph {
    display: block;
    width: 100%;
    height: 100%;
    min-height: 0;
    background: var(--flow-canvas, var(--surface-0));
    cursor: grab;
    touch-action: none;
    user-select: none;
  }

  .graph:active {
    cursor: grabbing;
  }

  .edges {
    fill: none;
    stroke: var(--border-strong);
    stroke-width: 1.2;
    opacity: 0.75;
  }

  .edges.dangling {
    stroke-dasharray: 3 3;
  }

  .edges.lit {
    stroke: var(--accent);
    stroke-width: 2;
    opacity: 1;
  }

  .arrows {
    fill: var(--border-strong);
    opacity: 0.75;
  }

  .arrows.lit {
    opacity: 1;
  }

  .node {
    cursor: pointer;
    outline: none;
  }

  .dot {
    fill: color-mix(in srgb, var(--node) 78%, var(--surface-0));
    stroke: color-mix(in srgb, var(--node) 60%, var(--text-1));
    stroke-width: 1.4;
    transition: opacity var(--duration-fast) var(--ease-ui);
  }

  .node.ghost .dot {
    fill: var(--surface-1);
    stroke-dasharray: 3 2;
  }

  .node.draft .dot {
    stroke-dasharray: 4 2;
  }

  .node.deprecated {
    opacity: 0.45;
  }

  .node.dim {
    opacity: 0.18;
  }

  .node.selected .dot,
  .node:focus-visible .dot {
    stroke: var(--text-1);
    stroke-width: 3;
  }

  .node.neighbor .dot {
    stroke-width: 2.4;
  }

  .mark.problem {
    fill: none;
    stroke: var(--warning);
    stroke-width: 1.5;
    stroke-dasharray: 2 2;
  }

  .badge {
    stroke: var(--surface-0);
    stroke-width: 1;
  }

  .badge.stale {
    fill: var(--warning);
  }

  .badge.verified {
    fill: var(--success);
  }

  .label {
    fill: var(--text-2);
    font-family: var(--font-sans);
    font-size: 11px;
    text-anchor: middle;
    paint-order: stroke;
    stroke: var(--surface-0);
    stroke-width: 3px;
    pointer-events: none;
  }

  .node.selected .label {
    fill: var(--text-1);
    font-weight: 650;
  }

  @media (prefers-reduced-motion: reduce) {
    .dot {
      transition: none;
    }
  }
</style>
