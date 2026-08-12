<script lang="ts">
  import App from "$ui/App.svelte";
  import type { AgentUiExtensions } from "$ui/lib/agentUiExtensions";
  import BrowserBridgeControl from "$chronicler/components/bridge/BrowserBridgeControl.svelte";
  import ChroniclerAudioWorkspaceExtension from "$chronicler/components/agent/ChroniclerAudioWorkspaceExtension.svelte";
  import { chroniclerToolPresentation } from "$chronicler/components/agent/chroniclerExtensions.svelte";
  import { chroniclerPresentationAdapter } from "$chronicler/state/chroniclerPresentation";

  const extensions: AgentUiExtensions = {
    headerControl: BrowserBridgeControl,
    workspaceComponent: ChroniclerAudioWorkspaceExtension,
    toolPresentation: chroniclerToolPresentation,
    presentation: chroniclerPresentationAdapter
  };
</script>

<App {extensions} />
