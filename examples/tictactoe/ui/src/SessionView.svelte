<!-- One session. The board and the ledger each follow it through their own AgentSession; the
     harness context below makes them share one connection and one state. -->
<script lang="ts">
  import { createHarnessContext, HttpTransport } from "@temporal-agent-harness/svelte";

  import Game from "./Game.svelte";
  import Ledger from "./Ledger.svelte";

  let { base, sessionId }: { base: string; sessionId: string } = $props();

  createHarnessContext();
  // Read once: SessionView is re-created when the server changes.
  // svelte-ignore state_referenced_locally
  const transport = new HttpTransport({ baseUrl: `${base}/api/` });
</script>

<div class="main">
  <Game {sessionId} {transport} />
  <Ledger {sessionId} {transport} />
</div>
