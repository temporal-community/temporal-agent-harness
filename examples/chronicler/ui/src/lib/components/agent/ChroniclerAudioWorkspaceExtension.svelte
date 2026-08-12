<script lang="ts">
  import type { AgentWorkspaceProps } from "$ui/lib/components/agent/AgentChatPanel.svelte";
  import ChroniclerAudioFeature from "./ChroniclerAudioFeature.svelte";
  import type { AudioGenerationPresentation } from "./audioPresentation";
  import {
    clearChroniclerAudioPresentation,
    setChroniclerAudioPresentation
  } from "./chroniclerExtensions.svelte";

  let { items, sessionId, following, closed, onSend }: AgentWorkspaceProps = $props();

  function updatePresentation(presentation: AudioGenerationPresentation | null): void {
    if (presentation) {
      setChroniclerAudioPresentation(presentation);
      return;
    }
    clearChroniclerAudioPresentation();
  }
</script>

<ChroniclerAudioFeature
  {items}
  {sessionId}
  {following}
  {closed}
  {onSend}
  onPresentationChange={updatePresentation}
/>
