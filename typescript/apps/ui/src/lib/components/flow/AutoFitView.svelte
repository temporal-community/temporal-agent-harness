<script lang="ts" module>
  import { getViewportForBounds } from "@xyflow/svelte";
  import type { FitViewOptions, Node, Viewport } from "@xyflow/svelte";

  export function nextFrame(): Promise<void> {
    return new Promise((resolve) => {
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(() => resolve());
      } else {
        setTimeout(resolve, 0);
      }
    });
  }

  /* xyflow's viewport promise can stay pending, which would strand the canvas hidden. */
  export function withFitBound(pending: Promise<boolean>): Promise<boolean> {
    let finished = false;
    let result = false;
    const arrived = pending.then(
      (value) => {
        finished = true;
        result = value;
      },
      () => {
        finished = true;
      }
    );
    const bound = (async () => {
      const start = typeof performance === "undefined" ? Date.now() : performance.now();
      const clock = () => (typeof performance === "undefined" ? Date.now() : performance.now());
      await nextFrame();
      await nextFrame();
      while (!finished && clock() - start < 250) await nextFrame();
    })();
    return Promise.race([arrived, bound]).then(() => (finished ? result : true));
  }

  /**
   * The framing for the sizes the nodes were laid out at, so the first fit needs
   * nothing from the DOM. xyflow's own fitView waits until it has measured every
   * node, which is a frame after they mount at best, and never while a stream
   * keeps handing it new ones. Null until the pane has a size.
   */
  export function knownFitViewport(
    nodes: readonly Node[],
    width: number,
    height: number,
    options: FitViewOptions<Node>
  ): Viewport | null {
    if (!width || !height || nodes.length === 0) return null;
    let minX = Number.POSITIVE_INFINITY;
    let minY = Number.POSITIVE_INFINITY;
    let maxX = Number.NEGATIVE_INFINITY;
    let maxY = Number.NEGATIVE_INFINITY;
    for (const node of nodes) {
      minX = Math.min(minX, node.position.x);
      minY = Math.min(minY, node.position.y);
      maxX = Math.max(maxX, node.position.x + (node.width ?? 0));
      maxY = Math.max(maxY, node.position.y + (node.height ?? 0));
    }
    return getViewportForBounds(
      { x: minX, y: minY, width: maxX - minX, height: maxY - minY },
      width,
      height,
      options.minZoom ?? 0.5,
      options.maxZoom ?? 2,
      options.padding ?? 0.1
    );
  }
</script>

<script lang="ts">
  import { onDestroy, tick } from "svelte";
  import { useStore, useSvelteFlow } from "@xyflow/svelte";

  interface Props {
    signature: string;
    /** Where the nodes are going, which mid-tween is not where the canvas has them. */
    graphNodes: readonly Node[];
    fitViewOptions: FitViewOptions<Node>;
    /** Snapshot paint and stream catch-up have reached their final frame. */
    ready: boolean;
    onSettled: () => void;
    /**
     * Deferred while true, and never dropped: a fit that ran against a graph
     * still travelling would chase it frame by frame. The signature is left
     * unconsumed, so the fit it asked for happens the moment this clears.
     */
    hold?: boolean;
  }

  let { signature, graphNodes, fitViewOptions, ready, onSettled, hold = false }: Props = $props();
  const { setViewport } = useSvelteFlow();
  const store = useStore();
  let previousSignature = "";
  let fitRequest = 0;
  let settled = false;
  /**
   * A reframe animates only when it stands alone. Fits that land on top of each
   * other cannot animate anyway — each one restarts the last, so the viewport
   * crawls a little way toward a target that keeps being replaced, and then jumps
   * the rest. That is what a page load looks like: the graph settles over a few
   * hundred ms and asks to be reframed several times on the way.
   *
   * The first fit is always immediate. Once the canvas has been shown, an
   * isolated later change gets its animation.
   */
  const quietMs = 300;
  let lastFitAt = typeof performance === "undefined" ? 0 : performance.now();

  onDestroy(() => {
    fitRequest += 1;
  });

  async function runFit(request: number): Promise<void> {
    await tick();
    /* Waits only for the pane to have a size, which a pane opened hidden or
       still being laid out does not have yet; superseded ends the wait. */
    let viewport = knownFitViewport(graphNodes, store.width, store.height, fitViewOptions);
    while (request === fitRequest && (!viewport || !store.panZoom)) {
      await nextFrame();
      viewport = knownFitViewport(graphNodes, store.width, store.height, fitViewOptions);
    }
    if (request !== fitRequest || !viewport) return;

    const alone = settled && performance.now() - lastFitAt > quietMs;
    lastFitAt = performance.now();
    await withFitBound(setViewport(viewport, { duration: alone ? 120 : 0 }));
    if (request !== fitRequest || settled) return;
    settled = true;
    onSettled();
  }

  $effect(() => {
    const nextSignature = signature;
    /* Read above, so this effect stays subscribed to it, and returned before
       previousSignature is written, so the held fit is owed rather than lost. */
    if (!ready || hold) return;
    if (!nextSignature || nextSignature === previousSignature) return;

    previousSignature = nextSignature;
    const request = ++fitRequest;

    void runFit(request).catch(() => undefined);
  });
</script>
