<script lang="ts">
  import { tick } from "svelte";
  import { useSvelteFlow } from "@xyflow/svelte";
  import type { FitViewOptions, Node } from "@xyflow/svelte";

  interface Props {
    signature: string;
    fitViewOptions: FitViewOptions<Node>;
    /**
     * Deferred while true, and never dropped: a fit that ran against a graph
     * still travelling would chase it frame by frame. The signature is left
     * unconsumed, so the fit it asked for happens the moment this clears.
     */
    hold?: boolean;
  }

  let { signature, fitViewOptions, hold = false }: Props = $props();
  const { fitView } = useSvelteFlow();
  let previousSignature = "";
  let fitRequest = 0;
  /**
   * A reframe animates only when it stands alone. Fits that land on top of each
   * other cannot animate anyway — each one restarts the last, so the viewport
   * crawls a little way toward a target that keeps being replaced, and then jumps
   * the rest. That is what a page load looks like: the graph settles over a few
   * hundred ms and asks to be reframed several times on the way.
   *
   * Mount seeds the clock, so the fits that arrive with the page are the ones that
   * count as crowded. A change that arrives later, into a graph the reader has been
   * looking at, gets its animation.
   */
  const quietMs = 300;
  let lastFitAt = typeof performance === "undefined" ? 0 : performance.now();

  function nextFrame(): Promise<void> {
    return new Promise((resolve) => {
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(() => resolve());
      } else {
        setTimeout(resolve, 0);
      }
    });
  }

  async function runFit(request: number): Promise<void> {
    await tick();
    await nextFrame();
    if (request !== fitRequest) return;

    await fitView({ ...fitViewOptions, duration: 0 });
    await nextFrame();
    if (request !== fitRequest) return;

    const alone = performance.now() - lastFitAt > quietMs;
    lastFitAt = performance.now();
    await fitView({ ...fitViewOptions, duration: alone ? 120 : 0 });
  }

  $effect(() => {
    const nextSignature = signature;
    /* Read above, so this effect stays subscribed to it, and returned before
       previousSignature is written, so the held fit is owed rather than lost. */
    if (hold) return;
    if (!nextSignature || nextSignature === previousSignature) return;

    previousSignature = nextSignature;
    const request = ++fitRequest;

    void runFit(request).catch(() => undefined);
  });
</script>
