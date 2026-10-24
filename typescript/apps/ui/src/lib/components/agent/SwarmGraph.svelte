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
  import { seededRandom } from "$lib/state/okfGraph";
  import {
    swarmShape,
    type SwarmGraph,
    type SwarmGraphEdge,
    type SwarmGraphNode
  } from "$lib/state/swarmView";

  /**
   * A swarm as a force-directed graph: members colored by phase and sized by role, with their
   * parent links, the peers each is waiting on, and the board lanes they claim from.
   *
   * Built like the OKF graph view: the simulation moves plain objects outside Svelte, and each
   * tick writes positions straight onto the elements already drawn — one transform per member,
   * one path per kind of edge — so a few hundred members tick without re-rendering. Positions
   * are kept by workflow id, so a member that starts settles in beside its parent and nothing
   * already placed jumps. The layout restarts only when the swarm's SHAPE changes (see
   * `swarmShape`); a poll that only moved phases recolors in place.
   */
  interface Props {
    graph: SwarmGraph;
    selectedId?: string | null;
  }

  let { graph, selectedId = $bindable(null) }: Props = $props();

  interface SimNode extends SimulationNodeDatum {
    id: string;
    node: SwarmGraphNode;
  }
  type SimLink = SimulationLinkDatum<SimNode> & { edge: SwarmGraphEdge };

  const WIDTH = 900;
  const HEIGHT = 640;

  const placed = new Map<string, SimNode>();
  let simulation: Simulation<SimNode, SimLink> | null = null;
  let shape = "";

  /** Where a member starts: beside its parent if that is placed, else on a fixed spiral. */
  function start(node: SwarmGraphNode, index: number): { x: number; y: number } {
    const parent = node.node.parent_id ? placed.get(node.node.parent_id) : undefined;
    const angle = index * 2.399963;
    if (parent?.x !== undefined && parent.y !== undefined) {
      return { x: parent.x + 24 * Math.cos(angle), y: parent.y + 24 * Math.sin(angle) };
    }
    const r = 14 * Math.sqrt(index + 0.5);
    return { x: r * Math.cos(angle), y: r * Math.sin(angle) };
  }

  const simNodes = $derived.by(() => {
    const live = new Set(graph.nodes.map((n) => n.id));
    for (const id of [...placed.keys()]) if (!live.has(id)) placed.delete(id);
    return graph.nodes.map((node, index) => {
      const existing = placed.get(node.id);
      if (existing) {
        existing.node = node;
        return existing;
      }
      const created: SimNode = { id: node.id, node, ...start(node, index) };
      placed.set(node.id, created);
      return created;
    });
  });

  const LINK_DISTANCE: Record<SwarmGraphEdge["kind"], number> = { parent: 34, peer: 90, board: 80 };
  const LINK_STRENGTH: Record<SwarmGraphEdge["kind"], number> = { parent: 0.7, peer: 0.04, board: 0.08 };

  $effect(() => {
    const nodes = simNodes;
    const links: SimLink[] = graph.edges.map((edge) => ({
      source: edge.source,
      target: edge.target,
      edge
    }));
    if (!simulation) {
      simulation = forceSimulation<SimNode, SimLink>()
        .randomSource(seededRandom(11))
        .force("charge", forceManyBody<SimNode>().strength(-55).distanceMax(320))
        .force("collide", forceCollide<SimNode>((d) => d.node.radius + 2.5))
        .force("x", forceX<SimNode>(0).strength(0.03))
        .force("y", forceY<SimNode>(0).strength(0.045))
        .on("tick", scheduleDraw);
    }
    simulation.nodes(nodes);
    simulation.force(
      "link",
      forceLink<SimNode, SimLink>(links)
        .id((d) => d.id)
        .distance((l) => LINK_DISTANCE[l.edge.kind])
        .strength((l) => LINK_STRENGTH[l.edge.kind])
    );
    const next = swarmShape(graph);
    if (next === shape) {
      // New phases or peer edges only: redraw where things already are.
      scheduleDraw();
      return;
    }
    shape = next;
    simulation.alpha(Math.max(simulation.alpha(), 0.3));
    if (prefersReducedMotion.current) {
      simulation.stop().tick(300);
      scheduleDraw();
    } else simulation.restart();
  });

  onDestroy(() => {
    simulation?.stop();
    if (pending !== 0) cancelAnimationFrame(pending);
  });

  const drawn = $derived.by(() => {
    const at = new Map(simNodes.map((n) => [n.id, n]));
    const focus = selectedId;
    return {
      nodes: simNodes.map((n) => ({ ...n.node, sim: n, selected: n.id === focus })),
      edges: graph.edges.map((edge) => ({
        ...edge,
        from: at.get(edge.source)!,
        to: at.get(edge.target)!,
        lit: edge.source === focus || edge.target === focus
      }))
    };
  });

  type DrawnEdge = (typeof drawn.edges)[number];

  const EDGE_LOOKS = ["parent", "board", "peer", "lit"] as const;
  type EdgeLook = (typeof EDGE_LOOKS)[number];

  const round = (value: number) => Math.round(value * 10) / 10;

  function edgePaths(edges: DrawnEdge[]): Record<EdgeLook, string> {
    const paths: Record<EdgeLook, string> = { parent: "", board: "", peer: "", lit: "" };
    for (const edge of edges) {
      const look: EdgeLook = edge.lit ? "lit" : edge.kind;
      paths[look] +=
        `M${round(edge.from.x ?? 0)} ${round(edge.from.y ?? 0)}` +
        `L${round(edge.to.x ?? 0)} ${round(edge.to.y ?? 0)}`;
    }
    return paths;
  }

  const edgeData = $derived(edgePaths(drawn.edges));

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

  function translate(node: SimNode): string {
    return `translate(${round(node.x ?? 0)} ${round(node.y ?? 0)})`;
  }

  let pending = 0;

  function scheduleDraw(): void {
    if (pending === 0) pending = requestAnimationFrame(draw);
  }

  function draw(): void {
    pending = 0;
    for (const node of drawn.nodes) nodeElements.get(node.id)?.setAttribute("transform", translate(node.sim));
    const paths = edgePaths(drawn.edges);
    for (const look of EDGE_LOOKS) edgeElements.get(look)?.setAttribute("d", paths[look]);
  }

  // ---------------------------------------------------------------- pan, zoom, select

  let svg = $state<SVGSVGElement | null>(null);
  let view = $state({ x: 0, y: 0, k: 1 });
  let pan: { startX: number; startY: number; viewX: number; viewY: number; moved: boolean } | null =
    null;

  function unitsPerPixel(): number {
    const rect = svg?.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return 1;
    return Math.max(WIDTH / rect.width, HEIGHT / rect.height);
  }

  function onDown(event: PointerEvent) {
    if (event.button !== 0) return;
    (event.currentTarget as Element).setPointerCapture(event.pointerId);
    pan = { startX: event.clientX, startY: event.clientY, viewX: view.x, viewY: view.y, moved: false };
  }

  function onMove(event: PointerEvent) {
    if (!pan) return;
    const dx = event.clientX - pan.startX;
    const dy = event.clientY - pan.startY;
    if (!pan.moved && Math.hypot(dx, dy) < 3) return;
    pan.moved = true;
    const scale = unitsPerPixel();
    view = { ...view, x: pan.viewX + dx * scale, y: pan.viewY + dy * scale };
  }

  function onUp(event: PointerEvent) {
    /* A press that did not pan is a click: on a member it selects it, on the canvas it clears. */
    if (pan && !pan.moved) {
      const target = (event.target as Element | null)?.closest("[data-member]");
      selectedId = target?.getAttribute("data-member") ?? null;
    }
    pan = null;
  }

  function onWheel(event: WheelEvent) {
    event.preventDefault();
    const k = Math.min(6, Math.max(0.2, view.k * Math.exp(-event.deltaY * 0.0015)));
    view = { ...view, k };
  }
</script>

<svg
  bind:this={svg}
  class="graph"
  viewBox={`${-WIDTH / 2} ${-HEIGHT / 2} ${WIDTH} ${HEIGHT}`}
  role="application"
  aria-label="Swarm graph"
  onpointerdown={onDown}
  onpointermove={onMove}
  onpointerup={onUp}
  onpointercancel={() => (pan = null)}
  onwheel={onWheel}
>
  <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
    {#each EDGE_LOOKS as look (look)}
      <path class={`edges ${look}`} d={edgeData[look]} {@attach keep(edgeElements, look)} />
    {/each}
    {#each drawn.nodes as node (node.id)}
      <g
        class="node"
        class:closed={node.closed}
        class:selected={node.selected}
        class:urgent={node.phase === "awaiting_approval" || node.phase === "awaiting_callback"}
        data-member={node.id}
        transform={translate(node.sim)}
        {@attach keep(nodeElements, node.id)}
        role="button"
        tabindex="0"
        aria-label={`${node.label}: ${node.closed ? "closed" : (node.phase ?? "no phase")}`}
        aria-pressed={node.selected}
        onkeydown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectedId = node.id;
          }
        }}
      >
        {#if node.role === "board"}
          <rect
            class="dot"
            x={-node.radius}
            y={-node.radius}
            width={node.radius * 2}
            height={node.radius * 2}
            rx="2"
            style={`--node: var(${node.tone})`}
          />
        {:else}
          <circle class="dot" r={node.radius} style={`--node: var(${node.tone})`} />
        {/if}
        {#if node.radius >= 7}
          <text class="label" y={node.radius + 11}>{node.label}</text>
        {/if}
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
    stroke-linecap: round;
  }

  .edges.parent {
    stroke: var(--border-strong);
    stroke-width: 1;
    opacity: 0.6;
  }

  .edges.board {
    stroke: var(--accent);
    stroke-width: 0.8;
    opacity: 0.35;
  }

  .edges.peer {
    stroke: var(--reasoning);
    stroke-width: 1.4;
    stroke-dasharray: 3 3;
    opacity: 0.85;
  }

  .edges.lit {
    stroke: var(--text-1);
    stroke-width: 1.8;
    opacity: 1;
  }

  .node {
    cursor: pointer;
    outline: none;
  }

  .dot {
    fill: color-mix(in srgb, var(--node) 80%, var(--surface-0));
    stroke: color-mix(in srgb, var(--node) 55%, var(--text-1));
    stroke-width: 1;
    transition: fill var(--duration-fast) var(--ease-ui);
  }

  .node.closed {
    opacity: 0.35;
  }

  .node.urgent .dot {
    stroke: var(--live);
    stroke-width: 2.4;
  }

  .node.selected .dot,
  .node:focus-visible .dot {
    stroke: var(--text-1);
    stroke-width: 2.6;
  }

  .label {
    fill: var(--text-2);
    font-family: var(--font-sans);
    font-size: 10px;
    text-anchor: middle;
    paint-order: stroke;
    stroke: var(--surface-0);
    stroke-width: 3px;
    pointer-events: none;
  }

  @media (prefers-reduced-motion: reduce) {
    .dot {
      transition: none;
    }
  }
</style>
