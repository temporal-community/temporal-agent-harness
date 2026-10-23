<script lang="ts">
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";

  interface Props {
    script: string;
    changedLines?: { from: number; to: number } | null;
  }

  let { script, changedLines = null }: Props = $props();
  const lines = $derived(script ? script.split("\n") : []);
</script>

{#if script}
  <section class="script-workspace" aria-label="Current script">
    <Copyable block value={script} label="Copy script">
      <pre dir="ltr">{#each lines as line, index (index)}<span class={{ changed: changedLines && index + 1 >= changedLines.from && index + 1 <= changedLines.to }}><i aria-hidden="true">{index + 1}</i><code>{line || " "}</code></span>{/each}</pre>
    </Copyable>
  </section>
{:else}
  <section class="script-workspace empty">
    <EmptyState title="No script yet">
      <p>The script appears here when an agent uses the editor tools.</p>
    </EmptyState>
  </section>
{/if}

<style>
  .script-workspace {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    height: 100%;
    padding: var(--pane-inset);
    overflow: hidden;
  }

  .script-workspace.empty {
    overflow: auto;
  }

  .script-workspace > :global(.copyable.block) {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
    border: 1px solid var(--code-block-border);
    background: var(--code-block-bg);
  }

  .script-workspace :global(.copyable.block > pre) {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    display: grid;
    align-content: start;
    margin: 0;
    padding: var(--gutter-tight) 0;
    overflow: auto;
    color: var(--code-block-text);
    background: transparent;
    font: var(--font-code)/1.55 var(--font-mono);
    tab-size: 4;
    white-space: pre;
  }

  pre > span {
    display: grid;
    grid-template-columns: 4ch minmax(0, 1fr);
    min-height: 1.55em;
    padding-inline-end: calc(var(--control-height-xs) + 8px);
  }

  pre > span.changed {
    background: var(--selected-bg);
  }

  i {
    padding-inline-end: 1ch;
    color: var(--text-3);
    font-style: normal;
    text-align: end;
    user-select: none;
  }

  code {
    direction: ltr;
    white-space: pre;
    unicode-bidi: isolate;
  }
</style>
