<script lang="ts">
  import { tick } from "svelte";
  import { useSvelteFlow } from "@xyflow/svelte";
  import type { FitViewOptions, Node } from "@xyflow/svelte";

  interface Props {
    signature: string;
    fitViewOptions: FitViewOptions<Node>;
  }

  let { signature, fitViewOptions }: Props = $props();
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
    if (!nextSignature || nextSignature === previousSignature) return;

    previousSignature = nextSignature;
    const request = ++fitRequest;

    void runFit(request).catch(() => undefined);
  });
</script>
