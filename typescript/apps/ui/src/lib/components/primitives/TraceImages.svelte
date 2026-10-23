<script lang="ts">
  import SafeImage from "$lib/components/primitives/SafeImage.svelte";
  import type { ToolImage } from "$lib/state/toolImages";

  /** The images a trace row's tool returned, drawn the same in the chat trace and in Logs. */
  let { images }: { images: readonly ToolImage[] } = $props();
</script>

{#if images.length}
  <div class="trace-images">
    {#each images as image (image.src)}
      <SafeImage src={image.src} alt={image.alt} />
    {/each}
  </div>
{/if}

<style>
  .trace-images {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }
</style>
