<script lang="ts">
  import TranscriptPanel, {
    type TranscriptFilter
  } from "$lib/components/agent/TranscriptPanel.svelte";
  import AgentStateFlow from "$lib/components/flow/AgentStateFlow.svelte";
  import LatencyWaterfall from "$lib/components/flow/LatencyWaterfall.svelte";
  import StepController from "$lib/components/flow/StepController.svelte";
  import HotkeyHelp from "$lib/components/flow/HotkeyHelp.svelte";
  import SessionControls from "$lib/components/chat/SessionControls.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import { Keyboard } from "@lucide/svelte";
  import AgentChatPanel from "$lib/components/agent/AgentChatPanel.svelte";
  import PaneRail, { type PaneDescription } from "$lib/panes/PaneRail.svelte";
  import PaneMinimap from "$lib/panes/PaneMinimap.svelte";
  import PaneLinkNotice from "$lib/panes/PaneLinkNotice.svelte";
  import { PANE_META } from "$lib/panes/registry";
  import { createAgentRunController } from "$lib/state/agentRun.svelte";
  import { createPaneStack, type Pane } from "$lib/state/paneStack.svelte";
  import {
    applyReplayAction,
    describeReplayKeyEvent,
    resolveReplayAction,
    type ReplaySurface
  } from "$lib/state/replayHotkeys";

  const SESSION_SYNC_INTERVAL_MS = 10_000;

  const run = createAgentRunController();
  const stack = createPaneStack();
  stack.hydrateFromQuery();

  let rail = $state<PaneRail | null>(null);
  let transcriptFilter = $state<TranscriptFilter>("all");
  let hotkeyHelpOpen = $state(false);

  $effect(() => {
    void run.initialize();
  });

  /* Sessions this UI did not start still belong in the list, so it is re-read on a
     timer instead of only when someone reaches for refresh. A hidden tab is
     skipped and caught up the moment it comes back: nobody is reading it, and
     every read costs the server a describe and a history scan per session. */
  $effect(() => {
    const syncIfVisible = () => {
      if (document.visibilityState === "visible") void run.syncSessions();
    };
    const timer = setInterval(syncIfVisible, SESSION_SYNC_INTERVAL_MS);
    document.addEventListener("visibilitychange", syncIfVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", syncIfVisible);
    };
  });

  /* The desk follows the session, so a switch neither carries one run's drill-ins
     into another nor throws away the desk being left. */
  $effect(() => {
    const sessionId = run.session?.workflow_id;
    if (sessionId) stack.enterSession(sessionId);
  });

  /* Layout and moment both live in the URL, so a link restores the whole desk.
     While following live there is no fixed moment to encode. A cursor that has
     not been applied yet stays in the URL so a reload does not lose it. */
  $effect(() => {
    if (stack.pendingCursor != null) {
      stack.writeQuery(stack.pendingCursor);
      return;
    }
    if (run.following) {
      stack.writeQuery(0);
      return;
    }
    stack.writeQuery(run.viewIndex);
  });

  /* A shared cursor can arrive before the stream has caught up to it. Parking on
     it has to clear `following`, or the next frame would drag the reader back to
     live and the link would look like it had been ignored. */
  $effect(() => {
    const pending = stack.pendingCursor;
    if (pending == null) return;
    if (run.total < pending) return;
    run.goTo(pending);
    run.following = false;
    stack.pendingCursor = null;
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

  function statusTone(status: typeof run.graph.status): string {
    if (status === "error") return "--error";
    if (status === "running") return "--accent";
    if (status === "replied") return "--success";
    return "--text-3";
  }

  /**
   * The four kinds this build renders. Every other kind keeps its registry label,
   * which is what a pane restored from a stale link is titled while its body
   * explains that it is not here yet.
   */
  function describePane(pane: Pane): PaneDescription {
    switch (pane.kind) {
      case "chat":
        return {
          title: (run.session ? run.runInfo.agentLabel : "") || "Agent chat",
          statusTone: pendingApprovalCount > 0 ? "--live" : null,
          statusLabel: pendingApprovalCount > 0 ? "needs you" : null
        };
      case "graph":
        return {
          title: "Session flow",
          statusTone: statusTone(run.graph.status),
          statusLabel: run.graph.status
        };
      case "logs":
        return { title: "Replay log" };
      case "latency":
        return { title: "Latency waterfall" };
      default:
        return { title: PANE_META[pane.kind].kindLabel };
    }
  }

  /* Arrow keys belong to whatever the user is typing in. */
  function isTextEntry(target: EventTarget | null): boolean {
    const element = target as HTMLElement | null;
    if (!element) return false;
    const tag = element.tagName;
    return (
      tag === "INPUT" ||
      tag === "TEXTAREA" ||
      tag === "SELECT" ||
      element.isContentEditable
    );
  }

  /* The keys act through `applyReplayAction`, which is also what the hotkey check drives, so
     what a key does here is what the check measures. */
  const replaySurface: ReplaySurface = {
    run,
    get helpOpen() {
      return hotkeyHelpOpen;
    },
    set helpOpen(open: boolean) {
      hotkeyHelpOpen = open;
    }
  };

  /* One window handler, because a component may only have one `<svelte:window>`,
     and two sets of bindings sharing it.

     The replay set is asked first: it brings its own guards — an IME mid-word, a
     text field, a focused scrubber, a control that claims the key through
     `aria-keyshortcuts` — which are finer than the `isTextEntry` test the pane
     bindings run on. The two sets cannot collide over the arrows, which is the
     only key they both spell: every pane binding carries Alt or Cmd/Ctrl+Shift,
     and `resolveReplayAction` declines anything modified.

     Escape is the one key both genuinely want. The help sheet is the topmost
     surface, so it takes Escape while it is up and a second press reaches the
     bleeding pane underneath; with the sheet closed the key falls straight
     through. */
  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.defaultPrevented) return;

    const replayAction = resolveReplayAction(describeReplayKeyEvent(event));
    if (replayAction != null && !(replayAction === "closeHelp" && !hotkeyHelpOpen)) {
      event.preventDefault();
      applyReplayAction(replayAction, replaySurface);
      return;
    }

    if (isTextEntry(event.target)) return;

    if (event.key === "Escape") {
      if (!stack.bleedingPane) return;
      event.preventDefault();
      stack.exitBleed();
      return;
    }

    /* Unmodified, because Cmd+F is the browser's and Alt+F is a menu. */
    if (event.key === "f" || event.key === "F") {
      if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      event.preventDefault();
      stack.toggleBleed();
      return;
    }

    const alongRail = event.key === "ArrowLeft" || event.key === "ArrowRight";
    const alongTabs = event.key === "ArrowUp" || event.key === "ArrowDown";
    if (!alongRail && !alongTabs) return;
    const delta = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;

    if ((event.metaKey || event.ctrlKey) && event.shiftKey) {
      if (!stack.focusedId) return;
      event.preventDefault();
      if (alongRail) stack.movePane(stack.focusedId, delta);
      else stack.movePaneAcross(stack.focusedId, delta);
      return;
    }

    if (!event.altKey || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    if (alongRail) stack.focusAlongRail(delta);
    else stack.focusAcross(delta);
    rail?.focusCurrent();
  }
</script>

<svelte:window onkeydown={handleWindowKeydown} />

<main class="app" class:bleed={stack.bleedingPane != null}>
  <!-- Two strips, and each answers one question: this one what you are looking
       at, the transport under the rail where in the run you are looking from.
       The link notice rides with the status line rather than as a third strip:
       it is a note on the arrangement that row describes, and it is only on
       screen while the link that opened the desk asked for something missing. -->
  <div class="chrome">
    <PaneMinimap {stack} describe={describePane}>
      {#snippet lead()}
        <SessionControls
          sessions={run.sessions}
          agents={run.agents}
          sessionId={run.runInfo.sessionId}
          connecting={run.connecting}
          sending={run.sending}
          creatingSession={run.creatingSession}
          refreshingSessions={run.refreshingSessions}
          closed={run.sessionClosed}
          closedWorkflowIds={run.closedWorkflowIds}
          error={run.connectionError}
          {pendingApprovalCount}
          onNewSession={(workflowType) => run.startNewSession(workflowType)}
          onSelectSession={(sessionId) => run.selectSession(sessionId)}
          onRefreshSessions={() => run.refreshSessions()}
        />
      {/snippet}

      <!-- The shortcuts are only real if they can be found. The branch this came
           from hung this off a topbar the pane rail replaced; the minimap is the
           strip that took that job, so it lands at the end of it. -->
      {#snippet trail()}
        <IconButton
          class="rail-icon"
          label="Replay keyboard shortcuts"
          tip={"Replay keyboard shortcuts\n?"}
          aria-expanded={hotkeyHelpOpen}
          data-tip-below
          data-tip-align="end"
          onclick={() => (hotkeyHelpOpen = !hotkeyHelpOpen)}
        >
          <Keyboard size={13} />
        </IconButton>
      {/snippet}
    </PaneMinimap>

    {#if stack.unknownPanes}
      <PaneLinkNotice
        report={stack.unknownPanes}
        onDismiss={() => stack.dismissUnknownPanes()}
      />
    {/if}
  </div>

  <PaneRail
    bind:this={rail}
    {stack}
    describe={describePane}
    bleedingId={stack.bleedingPane?.id ?? null}
  >
    {#snippet paneContent(pane)}
      <!-- The wrapper is what the narrow-column rules below hang off. It stands in
           for the old detail column: a definite-height box the four components can
           each fill with their own height: 100%. -->
      <div class="pane-content">
        {#if pane.kind === "graph"}
          <AgentStateFlow graph={run.graph} onNodeSelect={selectNode} />
        {:else if pane.kind === "chat"}
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
            onApproveTool={(workflowId, toolId, approved, remember) =>
              run.approveTool(workflowId, toolId, approved, remember)}
          />
        {:else if pane.kind === "latency"}
          <LatencyWaterfall
            timeline={run.stepTimeline}
            viewIndex={run.viewIndex}
            onScrub={(index) => run.goTo(index)}
          />
        {:else if pane.kind === "logs"}
          <TranscriptPanel
            groups={run.replayLog.groups}
            activeTurnNumber={run.currentLogRow?.turnNumber ?? null}
            activeRowId={run.currentLogRow?.id ?? null}
            activeOrdinal={run.currentLogRow?.ordinal ?? null}
            filter={transcriptFilter}
            onFilterChange={(next) => (transcriptFilter = next)}
          />
        {:else}
          <!-- A link can name a kind the registry knows and this build does not
               render. Saying so beats a pane that is simply blank. -->
          <p class="pane-todo">Not available yet.</p>
        {/if}
      </div>
    {/snippet}
  </PaneRail>

  <StepController
    viewIndex={run.viewIndex}
    total={run.total}
    playing={run.playing}
    following={run.following}
    playbackSpeed={run.playbackSpeed}
    currentEvent={run.currentLogRow}
    usage={run.usage}
    usageTimeline={run.usageTimeline}
    unmeasured={run.runUnmeasured}
    turnMarkers={run.turnMarkers}
    anomalyMarkers={run.anomalyMarkers}
    eventRows={run.fullReplayLog.rows}
    onPlay={() => run.play()}
    onPause={() => run.pause()}
    onStepBack={() => run.stepBack()}
    onStepForward={() => run.stepForward()}
    onPreviousTurn={() => {
      run.pause();
      run.previousTurn();
    }}
    onNextTurn={() => {
      run.pause();
      run.nextTurn();
    }}
    onSpeedChange={(speed) => run.setPlaybackSpeed(speed)}
    onJumpToLive={() => run.jumpToLive()}
    onReset={() => run.reset()}
    onScrub={(index) => run.goTo(index)}
  />
</main>

<HotkeyHelp open={hotkeyHelpOpen} onClose={() => (hotkeyHelpOpen = false)} />

<style>
  .app {
    height: 100vh;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto;
    background: var(--surface-0);
    color: var(--text-1);
  }

  /* One canvas, edge to edge — but the transport stays.

     The strip that says WHAT you are looking at can go: full screen is the
     reader saying they want the graph, and the graph names itself. The strip
     that says WHERE IN THE RUN you are looking from cannot, because this canvas
     is a point-in-time reading and the scrubber is the only thing that moves
     that point.

     Hidden rather than unmounted. The desk underneath keeps its columns, its
     widths and each pane's own scroll — and the canvas that is bleeding keeps
     its zoom, because it is the same element throughout. */
  .app.bleed {
    grid-template-rows: minmax(0, 1fr) auto;
  }

  .app.bleed .chrome {
    display: none;
  }

  /* One grid row, however many rows of chrome are in it, so the rail keeps the
     whole of what is left whether or not the notice is up. */
  .chrome {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  /* PaneShell's own body is a flex column, so `flex: 1 1 0` is what gives this a
     definite height for the components inside to measure their 100% against. */
  .pane-content {
    flex: 1 1 0;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }

  .pane-todo {
    margin: 0;
    padding: var(--gutter);
    color: var(--text-3);
    font-size: var(--font-md);
  }

  /* Re-homed from the deleted `.right-pane-body`. TranscriptPanel and
     LatencyWaterfall have only ever rendered inside that one narrow column and
     size themselves for a wide one otherwise; PaneShell offers no equivalent.
     Every class below is defined in exactly one of those two components, so
     hanging them off the shared wrapper cannot reach the graph or the chat. */
  .pane-content :global(.transcript) {
    width: 100%;
    height: 100%;
    min-width: 0;
    max-width: none;
    border-left: 0;
  }

  .pane-content :global(.waterfall-head) {
    padding: 12px;
  }

  .pane-content :global(.turns) {
    padding: 10px 12px 14px;
  }

  .pane-content :global(.turn-row) {
    grid-template-columns: minmax(0, 1fr);
    gap: 8px;
  }

  .pane-content :global(.turn-label) {
    grid-template-columns: auto auto;
  }

  .pane-content :global(.rollup) {
    width: 100%;
  }

  .pane-content :global(.roll) {
    flex: 1 1 120px;
  }
</style>
