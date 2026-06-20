<script lang="ts">
  import { Logs, MessageCircle, Timer } from "@lucide/svelte";
  import TranscriptPanel, {
    type TranscriptFilter
  } from "$lib/components/agent/TranscriptPanel.svelte";
  import AgentStateFlow from "$lib/components/flow/AgentStateFlow.svelte";
  import LatencyWaterfall from "$lib/components/flow/LatencyWaterfall.svelte";
  import StepController from "$lib/components/flow/StepController.svelte";
  import Badge from "$lib/components/primitives/Badge.svelte";
  import SessionControls from "$lib/components/support/SessionControls.svelte";
  import SupportAgentApp from "$lib/components/support/SupportAgentApp.svelte";
  import { createMockRunController } from "$lib/state/mockRun.svelte";

  type RightPanelView = "chat" | "latency" | "logs";

  const run = createMockRunController();
  let rightPanelView = $state<RightPanelView>("chat");
  let transcriptFilter = $state<TranscriptFilter>("all");

  $effect(() => {
    void run.initialize();
  });

  function selectNode(nodeId: string): void {
    const localNodeId = nodeId.split("::").at(-1) ?? nodeId;
    if (localNodeId === "model" || localNodeId === "reasoning") {
      transcriptFilter = "model";
    } else {
      transcriptFilter =
        localNodeId === "tool" || localNodeId === "approval" ? localNodeId : "all";
    }
    rightPanelView = "logs";
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

    <div class="session-slot">
      <SessionControls
        sessions={run.sessions}
        agents={run.agents}
        sessionId={run.runInfo.sessionId}
        connecting={run.connecting}
        sending={run.sending}
        creatingSession={run.creatingSession}
        error={run.connectionError}
        onNewSession={(workflowType) => run.startNewSession(workflowType)}
        onSelectSession={(sessionId) => run.selectSession(sessionId)}
      />
    </div>

    <div class="replay-status">
      <Badge label={run.graph.status} tone={run.graph.status === "idle" ? "done" : run.graph.status === "error" ? "error" : "model"} />
      <Badge label={`${run.viewIndex}/${run.total} events`} />
    </div>
  </header>

  <section class="workspace states">
    <div class="flow-pane">
      <AgentStateFlow graph={run.graph} onNodeSelect={selectNode} />
    </div>
    <aside class="right-pane" aria-label="Detail panel">
      <header class="right-pane-head">
        <div class="panel-tabs" role="group" aria-label="Right panel view">
          <button
            class={rightPanelView === "chat" ? "active" : ""}
            type="button"
            aria-pressed={rightPanelView === "chat"}
            onclick={() => (rightPanelView = "chat")}
          >
            <MessageCircle size={15} />
            Chat
          </button>
          <button
            class={rightPanelView === "latency" ? "active" : ""}
            type="button"
            aria-pressed={rightPanelView === "latency"}
            onclick={() => (rightPanelView = "latency")}
          >
            <Timer size={15} />
            Latency
          </button>
          <button
            class={rightPanelView === "logs" ? "active" : ""}
            type="button"
            aria-pressed={rightPanelView === "logs"}
            onclick={() => (rightPanelView = "logs")}
          >
            <Logs size={15} />
            Logs
          </button>
        </div>
      </header>

      <div class="right-pane-body">
        {#if rightPanelView === "chat"}
          <SupportAgentApp
            layout="embedded"
            showHeader={false}
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
            onApproveTool={(toolId, approved, remember) =>
              run.approveTool(toolId, approved, remember)}
          />
        {:else if rightPanelView === "latency"}
          <LatencyWaterfall
            timeline={run.stepTimeline}
            viewIndex={run.viewIndex}
            onScrub={(index) => run.goTo(index)}
          />
        {:else}
          <TranscriptPanel
            groups={run.replayLog.groups}
            activeTurnNumber={run.currentLogRow?.turnNumber ?? null}
            activeOffset={run.currentLogRow?.offset ?? null}
            filter={transcriptFilter}
            onFilterChange={(next) => (transcriptFilter = next)}
          />
        {/if}
      </div>
    </aside>
  </section>

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
    min-height: 62px;
    display: grid;
    grid-template-columns: minmax(180px, auto) minmax(0, 1fr) auto;
    gap: 14px;
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

  .panel-tabs {
    display: inline-flex;
    gap: 6px;
    padding: 3px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-0);
  }

  .panel-tabs button {
    min-width: 104px;
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

  .panel-tabs button.active {
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 13%, var(--surface-2));
  }

  .session-slot {
    min-width: 0;
    justify-self: center;
    width: min(100%, 760px);
  }

  .replay-status {
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
    grid-template-columns: minmax(480px, 1fr) minmax(420px, 540px);
  }

  .right-pane {
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    border-left: 1px solid var(--border);
    background: var(--surface-0);
  }

  .right-pane-head {
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-1);
  }

  .right-pane-body {
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }

  .right-pane-body :global(.transcript) {
    width: 100%;
    height: 100%;
    min-width: 0;
    max-width: none;
    border-left: 0;
  }

  .right-pane-body :global(.waterfall-head) {
    padding: 12px;
  }

  .right-pane-body :global(.turns) {
    padding: 10px 12px 14px;
  }

  .right-pane-body :global(.turn-row) {
    grid-template-columns: minmax(0, 1fr);
    gap: 8px;
  }

  .right-pane-body :global(.turn-label) {
    grid-template-columns: auto auto;
  }

  .right-pane-body :global(.rollup) {
    width: 100%;
  }

  .right-pane-body :global(.roll) {
    flex: 1 1 120px;
  }

  @media (max-width: 980px) {
    .topbar {
      grid-template-columns: 1fr;
      gap: 10px;
    }

    .session-slot {
      justify-self: stretch;
      width: 100%;
    }

    .replay-status {
      justify-self: start;
    }

    .states {
      display: flex;
      flex-direction: column;
    }

    .right-pane {
      width: 100%;
      min-width: 0;
      max-width: none;
      height: 44vh;
      border-left: 0;
      border-top: 1px solid var(--border);
    }

    .right-pane-head {
      justify-content: flex-start;
      overflow-x: auto;
    }
  }
</style>
