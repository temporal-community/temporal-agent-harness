<script lang="ts" module>
  export function nextFrame(): Promise<void> {
    return new Promise((resolve) => {
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(() => resolve());
      } else {
        setTimeout(resolve, 0);
      }
    });
  }

  /* xyflow's fitView promise can stay pending, which would strand the canvas hidden. */
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
</script>

<script lang="ts">
  import { onDestroy, tick } from "svelte";
  import { useSvelteFlow } from "@xyflow/svelte";
  import type { FitViewOptions, Node } from "@xyflow/svelte";

  interface Props {
    signature: string;
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

  let { signature, fitViewOptions, ready, onSettled, hold = false }: Props = $props();
  const { fitView, getNodes } = useSvelteFlow();
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
   * The first settled fit is always immediate. Once the canvas has been shown,
   * an isolated later change gets its animation.
   */
  const quietMs = 300;
  let lastFitAt = typeof performance === "undefined" ? 0 : performance.now();

  function measuredSignature(): string {
    const nodes = getNodes();
    if (
      nodes.length === 0 ||
      nodes.some(
        (node) =>
          typeof node.measured?.width !== "number" ||
          typeof node.measured?.height !== "number"
      )
    ) {
      return "";
    }
    return nodes
      .map((node) => `${node.id}:${node.measured?.width}x${node.measured?.height}`)
      .join("|");
  }

  async function waitForMeasuredNodes(request: number): Promise<boolean> {
    /* Svelte Flow writes measured dimensions after mounting each node. Two
       identical frames mean the snapshot handoff or remount did not merely
       leave us holding the previous node set's measurements. */
    let previous = "";
    while (request === fitRequest) {
      await nextFrame();
      const current = measuredSignature();
      if (current && current === previous) return true;
      previous = current;
    }
    return false;
  }

  onDestroy(() => {
    fitRequest += 1;
  });

  async function runFit(request: number): Promise<void> {
    const initial = !settled;
    await tick();
    if (!(await waitForMeasuredNodes(request))) return;

    const fitted = await withFitBound(fitView({ ...fitViewOptions, duration: 0 }));
    await nextFrame();
    if (
      request !== fitRequest ||
      !fitted ||
      !measuredSignature()
    ) return;

    const alone = settled && performance.now() - lastFitAt > quietMs;
    lastFitAt = performance.now();
    await withFitBound(fitView({ ...fitViewOptions, duration: alone ? 120 : 0 }));
    if (request !== fitRequest || !initial) return;
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
