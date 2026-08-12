<script lang="ts">
  import type { AgentToolActivityProps } from "../../lib/components/agent/AgentChatPanel.svelte";

  let {
    row,
    sessionId,
    following,
    closed,
    onSend
  }: AgentToolActivityProps = $props();
</script>

<output data-tool-activity-attachment>
  tool={row.toolName};session={sessionId};following={following};closed={closed};send={typeof onSend}
</output>
