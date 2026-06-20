<script lang="ts">
  import { tick } from "svelte";
  import { useSvelteFlow } from "@xyflow/svelte";

  interface Props {
    signature: string;
  }

  let { signature }: Props = $props();
  const { fitView } = useSvelteFlow();
  let previousSignature = "";
  let fitRequest = 0;

  $effect(() => {
    const nextSignature = signature;
    if (!nextSignature || nextSignature === previousSignature) return;

    previousSignature = nextSignature;
    const request = ++fitRequest;

    void tick()
      .then(() => {
        if (request !== fitRequest) return false;
        return fitView({ padding: 0.12, duration: 120 });
      })
      .catch(() => false);
  });
</script>
