<script lang="ts">
  import { Logs, MessageCircle, Timer } from "@lucide/svelte";
  import TranscriptPanel, {
    type TranscriptFilter
  } from "$lib/components/agent/TranscriptPanel.svelte";
  import AgentStateFlow from "$lib/components/flow/AgentStateFlow.svelte";
  import LatencyWaterfall from "$lib/components/flow/LatencyWaterfall.svelte";
  import StepController from "$lib/components/flow/StepController.svelte";
  import StatusChip, {
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";
  import SessionControls from "$lib/components/chat/SessionControls.svelte";
  import AgentChatPanel from "$lib/components/agent/AgentChatPanel.svelte";
  import { createAgentRunController } from "$lib/state/agentRun.svelte";

  type RightPanelView = "chat" | "latency" | "logs";

  const RIGHT_PANEL_MIN_WIDTH = 380;
  const RIGHT_PANEL_DEFAULT_WIDTH = 880;
  const RIGHT_PANEL_KEYBOARD_STEP = 24;
  const LEFT_PANE_MIN_WIDTH = 480;

  const run = createAgentRunController();
  let rightPanelView = $state<RightPanelView>("chat");
  let transcriptFilter = $state<TranscriptFilter>("all");
  let workspaceElement = $state<HTMLElement | null>(null);
  let rightPanelWidth = $state(RIGHT_PANEL_DEFAULT_WIDTH);
  let rightPanelResizing = $state(false);

  $effect(() => {
    void run.initialize();
  });

  const pendingApprovalCount = $derived.by(() => {
    const resolvedToolIds = new Set<string>();
    for (const row of run.fullReplayLog.rows) {
      if (row.event === "tool_approval_resolved" && row.toolId) {
        resolvedToolIds.add(row.toolId);
      }
    }
    return run.fullReplayLog.rows.filter(
      (row) =>
        row.event === "tool_approval_requested" &&
        row.toolId != null &&
        !resolvedToolIds.has(row.toolId)
    ).length;
  });

  function selectNode(nodeId: string): void {
    const localNodeId = nodeId.split("::").at(-1) ?? nodeId;
    if (localNodeId === "model" || localNodeId === "reasoning") {
      transcriptFilter = "model";
    } else {
      transcriptFilter =
        localNodeId.startsWith("tool:") || localNodeId === "tool-container"
          ? "tool"
          : localNodeId === "approval"
            ? "approval"
            : "all";
    }
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

  function graphStatusKind(status: typeof run.graph.status): StatusKind {
    if (status === "error") return "error";
    if (status === "running") return "thinking";
    if (status === "replied") return "complete";
    return "available";
  }

  function rightPanelMaxWidth(): number {
    const workspaceWidth = workspaceElement?.getBoundingClientRect().width ?? 0;
    if (!workspaceWidth) return Math.max(RIGHT_PANEL_MIN_WIDTH, rightPanelWidth);
    return Math.max(RIGHT_PANEL_MIN_WIDTH, workspaceWidth - LEFT_PANE_MIN_WIDTH);
  }

  function clampRightPanelWidth(width: number): number {
    return Math.min(
      Math.max(width, RIGHT_PANEL_MIN_WIDTH),
      rightPanelMaxWidth()
    );
  }

  function resizeRightPanelFromClientX(clientX: number): void {
    const rect = workspaceElement?.getBoundingClientRect();
    if (!rect) return;
    rightPanelWidth = Math.round(clampRightPanelWidth(rect.right - clientX));
  }

  function startRightPanelResize(event: PointerEvent): void {
    if (event.button !== 0 && event.pointerType !== "touch") return;
    event.preventDefault();
    rightPanelResizing = true;
    const handle = event.currentTarget as HTMLElement;
    handle.setPointerCapture(event.pointerId);
    resizeRightPanelFromClientX(event.clientX);
  }

  function moveRightPanelResize(event: PointerEvent): void {
    if (!rightPanelResizing) return;
    resizeRightPanelFromClientX(event.clientX);
  }

  function stopRightPanelResize(event: PointerEvent): void {
    rightPanelResizing = false;
    const handle = event.currentTarget as HTMLElement;
    if (handle.hasPointerCapture(event.pointerId)) {
      handle.releasePointerCapture(event.pointerId);
    }
  }

  function handleRightPanelResizeKeydown(event: KeyboardEvent): void {
    let nextWidth = rightPanelWidth;
    if (event.key === "ArrowLeft") {
      nextWidth += RIGHT_PANEL_KEYBOARD_STEP;
    } else if (event.key === "ArrowRight") {
      nextWidth -= RIGHT_PANEL_KEYBOARD_STEP;
    } else if (event.key === "Home") {
      nextWidth = RIGHT_PANEL_MIN_WIDTH;
    } else if (event.key === "End") {
      nextWidth = rightPanelMaxWidth();
    } else {
      return;
    }

    event.preventDefault();
    rightPanelWidth = Math.round(clampRightPanelWidth(nextWidth));
  }
</script>

<main class="app">
  <header class="topbar">
    <div class="brand">
      <img src="temporal-logo.svg" alt="Temporal logo" width="24" height="24" />
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
        closed={run.sessionClosed}
        closedWorkflowIds={run.closedWorkflowIds}
        error={run.connectionError}
        {pendingApprovalCount}
        onNewSession={(workflowType) => run.startNewSession(workflowType)}
        onSelectSession={(sessionId) => run.selectSession(sessionId)}
      />
    </div>

    <div class="replay-status">
      <StatusChip
        label={run.graph.status}
        kind={graphStatusKind(run.graph.status)}
        active={run.graph.status === "running"}
      />
      <StatusChip label={`${run.viewIndex}/${run.total} events`} kind="queued" compact />
    </div>
  </header>

  <section
    class={`workspace states ${rightPanelResizing ? "resizing" : ""}`}
    bind:this={workspaceElement}
    style={`--right-panel-width: ${rightPanelWidth}px`}
  >
    <div class="flow-pane">
      <AgentStateFlow graph={run.graph} onNodeSelect={selectNode} />
    </div>
    <aside class="right-pane" aria-label="Detail panel">
      <button
        type="button"
        class="resize-handle"
        aria-label="Resize detail panel"
        aria-keyshortcuts="ArrowLeft ArrowRight Home End"
        title="Resize detail panel"
        onpointerdown={startRightPanelResize}
        onpointermove={moveRightPanelResize}
        onpointerup={stopRightPanelResize}
        onpointercancel={stopRightPanelResize}
        onkeydown={handleRightPanelResizeKeydown}
      ></button>
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
          <AgentChatPanel
            layout="embedded"
            showHeader={false}
            items={run.chatTranscript}
            logs={run.fullReplayLog.rows}
            sessions={run.sessions}
            agentLabel={run.runInfo.agentLabel}
            sessionId={run.runInfo.sessionId}
            agents={run.agents}
            agentInterface={run.agentInterfaces[run.runInfo.sessionId] ?? []}
            operatorTargets={run.operatorTargets}
            currentAgentWorkflowType={run.session?.agent_workflow_type ?? null}
            connecting={run.connecting}
            sending={run.sending}
            creatingSession={run.creatingSession}
            closed={run.sessionClosed}
            closedWorkflowIds={run.closedWorkflowIds}
            error={run.connectionError}
            onSend={(message) => run.sendMessage(message)}
            onOperatorCommand={(name, arg, workflowId) =>
              run.executeOperatorCommand(name, arg, workflowId)}
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
            activeRowId={run.currentLogRow?.id ?? null}
            activeOrdinal={run.currentLogRow?.ordinal ?? null}
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
    background: color-mix(in srgb, var(--surface-1) 88%, black);
    box-shadow: 0 1px 0 rgb(255 255 255 / 0.03);
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
    gap: 4px;
    padding: 4px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--control-bg);
    box-shadow: inset 0 1px 0 rgb(255 255 255 / 0.04);
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
    font-weight: 650;
    transition:
      background 140ms ease,
      color 140ms ease,
      box-shadow 140ms ease;
  }

  .panel-tabs button:hover,
  .panel-tabs button:focus-visible {
    color: var(--text-1);
    background: var(--control-hover);
    outline: 0;
  }

  .panel-tabs button.active {
    color: color-mix(in srgb, var(--accent) 82%, white);
    background: color-mix(in srgb, var(--accent) 13%, var(--surface-2));
    box-shadow:
      inset 0 1px 0 rgb(255 255 255 / 0.06),
      0 0 0 1px color-mix(in srgb, var(--accent) 22%, transparent);
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
    grid-template-columns: minmax(480px, 1fr)
      clamp(380px, var(--right-panel-width, 880px), calc(100% - 480px));
  }

  .states.resizing,
  .states.resizing * {
    cursor: col-resize;
    user-select: none;
  }

  .right-pane {
    position: relative;
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    border-left: 1px solid var(--border);
    background: var(--surface-0);
  }

  .resize-handle {
    position: absolute;
    top: 0;
    bottom: 0;
    left: -6px;
    z-index: 6;
    width: 12px;
    border: 0;
    padding: 0;
    appearance: none;
    background: transparent;
    cursor: col-resize;
    outline: 0;
    touch-action: none;
  }

  .resize-handle::before {
    content: "";
    position: absolute;
    top: 0;
    bottom: 0;
    left: 5px;
    width: 2px;
    background: transparent;
    transition: background 120ms ease, box-shadow 120ms ease;
  }

  .resize-handle:hover::before,
  .resize-handle:focus-visible::before,
  .states.resizing .resize-handle::before {
    background: var(--accent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent);
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

    .resize-handle {
      display: none;
    }

    .right-pane-head {
      justify-content: flex-start;
      overflow-x: auto;
    }
  }
</style>
