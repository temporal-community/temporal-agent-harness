<script lang="ts">
  import { Workflow } from "@lucide/svelte";
  import { Background, BackgroundVariant, Controls, SvelteFlow, type Edge, type Node } from "@xyflow/svelte";
  import "@xyflow/svelte/dist/style.css";
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";

  /**
   * The State Flow before a run's first turn: the canvas with nothing on it, and the empty
   * state pinned to its corner. The root agent's box arrives with the first turn, together
   * with everything it frames, rather than standing empty until then.
   */
  interface Props {
    agentLabel: string;
    /** Names of the handlers the agent declares. */
    accepts: string[];
  }

  let { agentLabel, accepts }: Props = $props();
  let nodes = $state.raw<Node[]>([]);
  let edges = $state.raw<Edge[]>([]);
  let textWidth = $state(0);
  let textHeight = $state(0);
</script>

<div class="flow-empty" style:--clear-w={`${textWidth}px`} style:--clear-h={`${textHeight}px`}>
  <SvelteFlow
    bind:nodes
    bind:edges
    preventScrolling={false}
    colorMode="dark"
    nodesDraggable={false}
    nodesConnectable={false}
    elementsSelectable={false}
    proOptions={{ hideAttribution: true }}
  >
    <Controls showLock={false} />
    <!-- AgentStateFlow's pattern, so the dots don't change when the first turn swaps it in. -->
    <Background
      variant={BackgroundVariant.Dots}
      gap={18}
      size={1}
      bgColor="var(--flow-canvas)"
      patternColor="var(--flow-dots)"
    />
  </SvelteFlow>
  <div class="pinned" bind:clientWidth={textWidth} bind:clientHeight={textHeight}>
    <EmptyState title="No turns yet" icon={Workflow}>
      <p>
        Each turn {agentLabel || "the agent"} takes is drawn here as a graph of its model calls and
        tool runs.
      </p>
      {#if accepts.length > 0}
        <p>
          It accepts
          {#each accepts as name, index (name)}<code>{name}</code>{index < accepts.length - 1
              ? ", "
              : ""}{/each}.
        </p>
      {/if}
    </EmptyState>
  </div>
</div>

<style>
  .flow-empty {
    position: relative;
    width: 100%;
    height: 100%;
    background: var(--flow-canvas);
  }

  /* Pinned in screen space, and transparent to the pointer, so a drag that starts on the
     text still pans the canvas under it. */
  .pinned {
    position: absolute;
    top: 0;
    left: 0;
    z-index: 5;
    max-width: 100%;
    pointer-events: none;
  }

  /* The dots thin out in an ellipse around the text rather than stopping at a box, which
     draws an edge. Only the pattern is masked; the canvas colour under it is the same. */
  .flow-empty :global(.svelte-flow__background) {
    mask-image: radial-gradient(
      ellipse calc(var(--clear-w) * 1.2) calc(var(--clear-h) * 1.5) at 0 0,
      transparent 62%,
      #000 100%
    );
  }
</style>
