<script lang="ts">
  import { onDestroy } from "svelte";
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
   * each tick bumps one counter that redraws. Positions are kept by concept id, so when the
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

  // Not reactive: the simulation mutates these in place, and `frame` says when to redraw.
  const placed = new Map<string, SimNode>();
  let simulation: Simulation<SimNode, SimLink> | null = null;
  let frame = $state(0);

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
        .on("tick", () => (frame += 1));
    }
    simulation.nodes(nodes);
    simulation.force(
      "link",
      forceLink<SimNode, SimLink>(links)
        .id((d) => d.id)
        .distance(105)
        .strength(0.35)
    );
    // Warm restart: what was already placed barely moves; new nodes settle in.
    simulation.alpha(Math.max(simulation.alpha(), 0.35)).restart();
  });

  onDestroy(() => simulation?.stop());

  const hidden = $derived(new Set(hiddenTypes));

  /** What is drawn this frame. */
  const drawn = $derived.by(() => {
    void frame;
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
        x: n.x ?? 0,
        y: n.y ?? 0,
        dim: !matches(n.node, query),
        selected: n.id === focus,
        neighbor: neighbors.has(n.id)
      })),
      edges: elements.edges
        .filter((e) => shown.has(e.source) && shown.has(e.target))
        .map((e) => ({
          ...e,
          x1: at.get(e.source)?.x ?? 0,
          y1: at.get(e.source)?.y ?? 0,
          x2: at.get(e.target)?.x ?? 0,
          y2: at.get(e.target)?.y ?? 0,
          lit: e.source === focus || e.target === focus
        }))
    };
  });

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
  <defs>
    <marker id="okf-arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" class="arrow" />
    </marker>
  </defs>
  <g bind:this={layer} transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
    {#each drawn.edges as edge (edge.id)}
      {@const dx = edge.x2 - edge.x1}
      {@const dy = edge.y2 - edge.y1}
      {@const length = Math.hypot(dx, dy) || 1}
      {@const target = drawn.nodes.find((n) => n.id === edge.target)}
      {@const inset = (target?.radius ?? 8) + 3}
      <line
        class="edge"
        class:lit={edge.lit}
        class:dangling={edge.dangling}
        x1={edge.x1}
        y1={edge.y1}
        x2={edge.x2 - (dx / length) * inset}
        y2={edge.y2 - (dy / length) * inset}
        marker-end="url(#okf-arrow)"
      />
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
        transform={`translate(${node.x} ${node.y})`}
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

  .arrow {
    fill: var(--border-strong);
  }

  .edge {
    stroke: var(--border-strong);
    stroke-width: 1.2;
    opacity: 0.75;
  }

  .edge.dangling {
    stroke-dasharray: 3 3;
  }

  .edge.lit {
    stroke: var(--accent);
    stroke-width: 2;
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
</style>
