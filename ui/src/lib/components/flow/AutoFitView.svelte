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

    await fitView({ ...fitViewOptions, duration: 120 });
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
