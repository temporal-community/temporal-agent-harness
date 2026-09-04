<script lang="ts">
  import MarkdownMessage from "$lib/components/chat/MarkdownMessage.svelte";

  const block = [
    "```javascript",
    'const providerSelect = document.getElementById("provider-select"), modelSelect = document.getElementById("model-select"), temperatureField = document.getElementById("temperature-field");',
    "```"
  ].join("\n");
</script>

<div class="shots">
  <section data-shot="wrapped" class="col">
    <MarkdownMessage text={block} />
  </section>
  <section data-shot="scrolls" class="col forced-pre">
    <MarkdownMessage text={block} />
  </section>
</div>

<style>
  .shots {
    display: grid;
    gap: 24px;
    padding: 24px;
    background: var(--surface-0);
  }

  .col {
    width: 420px;
    display: grid;
    min-width: 0;
  }

  /* The case the wrap fix does not reach: white-space overridden back to pre,
     which is also what the currently shipped dist bundle does. */
  .forced-pre :global(pre code) {
    white-space: pre !important;
  }
</style>
