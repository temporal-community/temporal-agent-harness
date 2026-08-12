<script lang="ts">
  import type { AgentWorkspaceProps } from "../../lib/components/agent/AgentChatPanel.svelte";

  let {
    items,
    sessionId,
    following,
    closed,
    onSend
  }: AgentWorkspaceProps = $props();
</script>

<output data-workspace-extension>
  items={items.length};session={sessionId};following={following};closed={closed};send={typeof onSend}
</output>
