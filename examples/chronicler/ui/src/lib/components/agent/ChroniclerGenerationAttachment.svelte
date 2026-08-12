<script lang="ts">
  import type { AgentToolActivityProps } from "$lib/components/agent/AgentChatPanel.svelte";
  import AudioGenerationCard from "./AudioGenerationCard.svelte";
  import {
    currentChroniclerAudioPresentation,
    isCurrentChroniclerAudioPresentationRow
  } from "./chroniclerExtensions.svelte";

  let { row }: AgentToolActivityProps = $props();
  const presentation = $derived(currentChroniclerAudioPresentation());
  const isSelectedRow = $derived(isCurrentChroniclerAudioPresentationRow(row));
</script>

{#if presentation && isSelectedRow}
  <div class="audio-generation-activity">
    <AudioGenerationCard {...presentation} />
  </div>
{/if}
