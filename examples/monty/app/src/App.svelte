<script lang="ts">
  import { Activity, MessageCircle, Logs, Timer } from "@lucide/svelte";
  import TranscriptPanel, {
    type TranscriptFilter
  } from "$lib/components/agent/TranscriptPanel.svelte";
  import AgentStateFlow from "$lib/components/flow/AgentStateFlow.svelte";
  import LatencyWaterfall from "$lib/components/flow/LatencyWaterfall.svelte";
  import StepController from "$lib/components/flow/StepController.svelte";
  import Badge from "$lib/components/primitives/Badge.svelte";
  import SupportAgentApp from "$lib/components/support/SupportAgentApp.svelte";
  import { createMockRunController } from "$lib/state/mockRun.svelte";

  type AppView = "app" | "states" | "timeline" | "chat";

  function initialView(): AppView {
    if (typeof window === "undefined") return "app";
    return window.location.pathname === "/states" ? "states" : "app";
  }

  const run = createMockRunController();
  let view = $state<AppView>(initialView());
  let transcriptFilter = $state<TranscriptFilter>("all");

  $effect(() => {
    void run.initialize();
  });

  function selectNode(nodeId: string): void {
    const localNodeId = nodeId.split("::").at(-1) ?? nodeId;
    if (localNodeId === "model" || localNodeId === "reasoning") {
      transcriptFilter = "model";
      return;
    }
    transcriptFilter =
      localNodeId === "tool" || localNodeId === "approval" ? localNodeId : "all";
  }

  function startedAtLabel(seconds: number): string {
    if (!seconds) return "";
    return new Date(seconds * 1000).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    });
  }
</script>

<main class="app">
  <header class="topbar">
    <div class="brand">
    <img src="/temporal-logo.svg" alt="Temporal logo" width="24" height="24" />
      <div class="brand-text">
        <h1>Agentic Harness</h1>
        <p>
          {#if run.runInfo.startedAt}
            {startedAtLabel(run.runInfo.startedAt)}
          {/if}
        </p>
      </div>
    </div>

    <nav aria-label="View switcher">
      <button
        class={view === "app" ? "active" : ""}
        type="button"
        onclick={() => (view = "app")}
      >
        <MessageCircle size={15} />
        App
      </button>
      <button
        class={view === "states" ? "active" : ""}
        type="button"
        onclick={() => (view = "states")}
      >
        <Activity size={15} />
        States
      </button>
      <button
        class={view === "timeline" ? "active" : ""}
        type="button"
        onclick={() => (view = "timeline")}
      >
        <Timer size={15} />
        Latency
      </button>
      <button
        class={view === "chat" ? "active" : ""}
        type="button"
        onclick={() => (view = "chat")}
      >
        <Logs size={15} />
        Logs
      </button>
    </nav>

    <div class="status">
      <Badge label={run.graph.status} tone={run.graph.status === "idle" ? "done" : run.graph.status === "error" ? "error" : "model"} />
      <Badge label={`${run.viewIndex}/${run.total} events`} />
    </div>
  </header>

  {#if view === "app"}
    <section class="workspace app-workspace">
      <SupportAgentApp
        items={run.supportTranscript}
        logs={run.fullReplayLog.rows}
        sessions={run.sessions}
        agentLabel={run.runInfo.agentLabel}
        sessionId={run.runInfo.sessionId}
        agents={run.agents}
        currentAgentWorkflowType={run.session?.agent_workflow_type ?? null}
        connecting={run.connecting}
        sending={run.sending}
        creatingSession={run.creatingSession}
        error={run.connectionError}
        onSend={(message) => run.sendMessage(message)}
        onNewSession={(workflowType) => run.startNewSession(workflowType)}
        onSelectSession={(sessionId) => run.selectSession(sessionId)}
        onApproveTool={(toolId, approved) => run.approveTool(toolId, approved)}
      />
    </section>
  {:else if view === "states"}
    <section class="workspace states">
      <div class="flow-pane">
        <AgentStateFlow
          graph={run.graph}
          onNodeSelect={selectNode}
        />
      </div>
      <TranscriptPanel
        groups={run.replayLog.groups}
        activeTurnNumber={run.currentLogRow?.turnNumber ?? null}
        activeOffset={run.currentLogRow?.offset ?? null}
        filter={transcriptFilter}
        onFilterChange={(next) => (transcriptFilter = next)}
      />
    </section>
  {:else if view === "timeline"}
    <section class="workspace timeline">
      <LatencyWaterfall
        timeline={run.stepTimeline}
        viewIndex={run.viewIndex}
        onScrub={(index) => run.goTo(index)}
      />
    </section>
  {:else}
    <section class="workspace transcript-only">
      <TranscriptPanel
        groups={run.replayLog.groups}
        activeTurnNumber={run.currentLogRow?.turnNumber ?? null}
        activeOffset={run.currentLogRow?.offset ?? null}
        filter={transcriptFilter}
        onFilterChange={(next) => (transcriptFilter = next)}
      />
    </section>
  {/if}

  {#if view !== "app"}
    <StepController
      viewIndex={run.viewIndex}
      total={run.total}
      playing={run.playing}
      following={run.following}
      playbackSpeed={run.playbackSpeed}
      currentEvent={run.currentLogRow}
      usage={run.usage}
      usageTimeline={run.usageTimeline}
      turnMarkers={run.turnMarkers}
      anomalyMarkers={run.anomalyMarkers}
      onPlay={() => run.play()}
      onPause={() => run.pause()}
      onStepBack={() => run.stepBack()}
      onStepForward={() => run.stepForward()}
      onSpeedChange={(speed) => run.setPlaybackSpeed(speed)}
      onJumpToLive={() => run.jumpToLive()}
      onReset={() => run.reset()}
      onScrub={(index) => run.goTo(index)}
      onPreviousMarker={() => run.previousMarker()}
      onNextMarker={() => run.nextMarker()}
    />
  {/if}
</main>

<style>
  .app {
    height: 100vh;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto;
    background: var(--surface-0);
    color: var(--text-1);
  }

  .topbar {
    min-height: 58px;
    display: grid;
    grid-template-columns: minmax(240px, 1fr) auto minmax(220px, 1fr);
    gap: 18px;
    align-items: center;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-1);
  }

  .brand {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--accent);
  }

  .brand-text {
    min-width: 0;
  }

  h1 {
    margin: 0;
    color: var(--text-1);
    font-size: 14px;
    line-height: 1.2;
  }

  p {
    margin: 2px 0 0;
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    align-items: center;
    color: var(--text-3);
    font-size: 12px;
  }

  nav {
    display: inline-flex;
    gap: 6px;
    padding: 3px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-0);
  }

  nav button {
    min-width: 112px;
    height: 32px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    border: 0;
    border-radius: 6px;
    color: var(--text-2);
    background: transparent;
    cursor: pointer;
    font: inherit;
    font-size: 12px;
  }

  nav button.active {
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 13%, var(--surface-2));
  }

  .status {
    justify-self: end;
    display: inline-flex;
    gap: 8px;
    align-items: center;
  }

  .workspace {
    min-height: 0;
    display: flex;
    overflow: hidden;
  }

  .flow-pane {
    min-width: 0;
    flex: 1;
  }

  .states {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 420px;
  }

  .states :global(.transcript) {
    width: 420px;
    min-width: 420px;
    max-width: 420px;
  }

  .timeline {
    background: var(--surface-0);
  }

  .app-workspace {
    background: var(--surface-0);
  }

  .transcript-only {
    justify-content: center;
    background: var(--surface-0);
  }

  .transcript-only :global(.transcript) {
    width: min(820px, 100%);
    max-width: none;
    border-left: 1px solid var(--border);
    border-right: 1px solid var(--border);
  }

  @media (max-width: 980px) {
    .topbar {
      grid-template-columns: 1fr;
      gap: 10px;
    }

    nav,
    .status {
      justify-self: start;
    }

    .states {
      display: flex;
      flex-direction: column;
    }

    .states :global(.transcript) {
      width: 100%;
      min-width: 0;
      max-width: none;
      height: 38vh;
      border-left: 0;
      border-top: 1px solid var(--border);
    }
  }
</style>
