<script lang="ts">
  import TranscriptPanel, {
    type TranscriptFilter
  } from "$lib/components/agent/TranscriptPanel.svelte";
  import AgentStateFlow from "$lib/components/flow/AgentStateFlow.svelte";
  import LatencyWaterfall from "$lib/components/flow/LatencyWaterfall.svelte";
  import UsageReading from "$lib/components/flow/UsageReading.svelte";
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

  /**
   * The bottom drawer: a second rail, not a bespoke sheet.
   *
   * `PaneRail` is height-agnostic — a row of columns that fills whatever box its
   * parent gives it — so a fourth grid row of the app hands the drawer tabs,
   * splits, folds, gutters, drop marks and the "Open space" launcher without a
   * line of new pane chrome. It opens empty, and its keys are suffixed so the
   * links this console has always written keep their exact spelling.
   */
  const drawer = createPaneStack({ queryPrefix: "2", initial: [] });
  drawer.hydrateFromQuery();

  /* Both the smallest drawer worth drawing and the point below which it shuts, which
     is one constant on purpose: the drawer is either shut or tall enough for a trace,
     with no range in between where a header and a scale note are the only things that
     fit. Snapping across it also means no band a drag can sit in while chrome
     flickers in and out. */
  const DRAWER_MIN_H = 96;
  /* Only reached when there is nothing measurable to fit to — a drawer holding a
     pane with no natural height, or opened before its trace has loaded. */
  const DRAWER_DEFAULT_H = 340;
  /* Not pixel-tight. A fitted drawer sitting exactly on its last row reads as
     clipped rather than fitted, sub-pixel rounding is enough to raise a scrollbar
     on a trace that fits, and during a live run this is where the next turn
     appears before anything has to move. */
  const DRAWER_FIT_SLACK = 12;
  /* An unattended fit stops well short of the row's own ceiling. Nothing that happens
     without being asked for should be able to take three fifths of the window; the
     reader who wants that drags for it, and the gutter goes all the way to 60vh. */
  const DRAWER_FIT_MAX_FRACTION = 0.5;

  let rail = $state<PaneRail | null>(null);
  let drawerRail = $state<PaneRail | null>(null);
  let drawerElement = $state<HTMLElement | null>(null);
  let drawerHeight = $state(DRAWER_DEFAULT_H);
  let resizingDrawer = $state(false);
  /* Which of the two rails the arrows, F and Escape act on: the last one touched,
     because both are on screen at once and neither is "the" rail any more. */
  let drawerActive = $state(false);
  let transcriptFilter = $state<TranscriptFilter>("all");
  let hotkeyHelpOpen = $state(false);

  const activeStack = $derived(drawerActive && drawer.groups.length > 0 ? drawer : stack);
  const activeRail = $derived(drawerActive && drawer.groups.length > 0 ? drawerRail : rail);

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

  /* The drawer's arrangement is part of the desk a link restores. The moment is
     not: there is one replay cursor and the rail above already carries it. */
  $effect(() => {
    drawer.writeQuery(0);
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

  /**
   * Every key in the console, resolved through one table.
   *
   * The pane bindings used to be an if-chain here, beside the call into
   * `replayHotkeys`. Folding them in cost about the lines it saved and bought
   * the two things the chain could not have: the help overlay renders from the
   * table, so Alt+Arrows and F are now written down, and
   * `check-replay-hotkeys.mjs` drives the same resolver, so they are now
   * checked. Which is what the inline chain was missing when Alt+Left quietly
   * took the word-jump out of the chat composer.
   *
   * What is left here is the two things a plain object cannot know: which of the
   * two rails the reader last touched, and how to put DOM focus back on the pane
   * a walk landed on.
   */
  const replaySurface: ReplaySurface = {
    run,
    get helpOpen() {
      return hotkeyHelpOpen;
    },
    set helpOpen(open: boolean) {
      hotkeyHelpOpen = open;
    },
    rail: {
      focus(axis, delta) {
        if (axis === "along") activeStack.focusAlongRail(delta);
        else activeStack.focusAcross(delta);
        activeRail?.focusCurrent();
      },
      move(axis, delta) {
        const id = activeStack.focusedId;
        if (!id) return;
        if (axis === "along") activeStack.movePane(id, delta);
        else activeStack.movePaneAcross(id, delta);
      },
      toggleBleed: () => activeStack.toggleBleed(),
      /* Both, not the active one. A bled drawer hides the rail above it, so the
         only thing left to click is the transport — which moves the active rail
         back to a stack that is not the one holding the screen. Escaping the
         rail you are not looking at is a way to be stuck full-screen. */
      exitBleed: () => {
        stack.exitBleed();
        drawer.exitBleed();
      }
    }
  };

  /* Same reason: whether Escape has anything to do is asked of the desk, not of
     whichever rail the reader last touched. */
  const bleeding = $derived(stack.bleedingPane != null || drawer.bleedingPane != null);

  /* One window handler, because a component may only have one `<svelte:window>`
     — and now one binding table behind it. */
  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.defaultPrevented) return;

    const action = resolveReplayAction(
      describeReplayKeyEvent(event, { helpOpen: hotkeyHelpOpen, bleeding })
    );
    if (action == null) return;
    event.preventDefault();
    applyReplayAction(action, replaySurface);
  }

  /* Which rail the keys act on, read off where the reader last put their hands
     rather than held as another thing to keep true. Focus alone is not enough:
     clicking a pane body moves the pointer's attention without always moving
     DOM focus out of the rail above. */
  function noteRail(event: Event): void {
    const node = event.target;
    if (!(node instanceof Element)) return;
    drawerActive = node.closest(".drawer") != null;
  }

  /* Same pointer-capture shape as the rail's own column gutter, on the other axis.
     The drawer is the last row, so its bottom is pinned to the floor of the window
     and its height is the distance from the pointer down to it — the same arithmetic
     as when it sat above the transport, for a different reason. */
  function resizeDrawerFrom(event: PointerEvent): void {
    const rect = drawerElement?.getBoundingClientRect();
    if (!rect) return;
    const height = Math.round(rect.bottom - event.clientY);
    /* Snap shut rather than bottoming out on a strip of leftover chrome: a drawer too
       short for a trace has nothing in it worth the header telling you so. Zero is
       the whole signal — the row collapses out of the grid on its own. */
    drawerHeight = height < DRAWER_MIN_H ? 0 : height;
    /* From here the height is the reader's, and fitting stops second-guessing it. */
    drawerSized = true;
  }

  /**
   * The height the drawer takes when it is opened rather than dragged: tall enough
   * for the trace it holds, and no taller.
   *
   * Measured off the DOM and never from `drawerHeight`, so the row this sets cannot
   * feed back into the measurement that sets it. Pure CSS was the first choice and
   * does not work here: sizing the grid row to `auto` collapses the drawer to a
   * single pixel, because `PaneRail` and `PaneShell` fill their box top-down and an
   * indefinite row leaves every one of them nothing to fill.
   */
  function fitDrawerToContent(): number | null {
    const turns = drawerElement?.querySelector(".turns");
    const last = turns?.lastElementChild;
    if (!drawerElement || !turns || !last) return null;

    /* `scrollHeight` is useless in the direction that matters, because it never
       reports less than the box: a trace with room to spare measures as exactly the
       height it was already given, and the drawer would only ever grow. The last
       row's own bottom edge is the honest reading of content shorter than its
       scroller, which is the case this whole function exists for. */
    const box = turns.getBoundingClientRect();
    const content =
      last.getBoundingClientRect().bottom -
      box.top +
      turns.scrollTop +
      Number.parseFloat(getComputedStyle(turns).paddingBottom || "0");
    /* Everything that is not the scroller — the scale strip, the pane's padding,
       the gutter — taken as one lump so this stays true if that chrome changes. */
    const chrome = drawerElement.getBoundingClientRect().height - box.height;

    drawerHeight = Math.round(
      Math.min(
        Math.max(chrome + content + DRAWER_FIT_SLACK, DRAWER_MIN_H),
        window.innerHeight * DRAWER_FIT_MAX_FRACTION
      )
    );
    return drawerHeight;
  }

  /**
   * Measure again until the answer stops changing, then never again.
   *
   * This is the whole of the refit policy, and it exists because the two failures are
   * opposite. Freezing on the first measurement locks a 102px drawer around a trace
   * that has not arrived, because a link naming the drawer restores the pane before
   * the session behind it streams anything. Subscribing to content height instead
   * grows the drawer by a row every turn until it has eaten the rail, which is not a
   * height anyone asked for. Convergence is the thing both want: a trace still
   * arriving measures differently every time and keeps the fit honest, a trace that
   * has arrived measures the same twice and the question is closed for good. A turn
   * added later is content changing after the answer was settled, and is ignored.
   *
   * ponytail: a poll, not an observer. Ceiling is `DRAWER_FIT_TRIES` measurements —
   * a run that changes shape faster than that for eight seconds straight stops
   * getting fitted rather than following forever. A ResizeObserver on the last row
   * would be exact; this is four lines and the same answer everywhere it matters.
   */
  function settleDrawerFit(previous: number | null, tries = 0): void {
    if (drawerFitTimer != null) clearTimeout(drawerFitTimer);
    drawerFitTimer = window.setTimeout(() => {
      drawerFitTimer = null;
      if (drawerSized || tries >= DRAWER_FIT_TRIES) return;
      const measured = fitDrawerToContent();
      if (measured != null && measured === previous) return;
      settleDrawerFit(measured, tries + 1);
    }, DRAWER_FIT_SETTLE_MS);
  }

  function startDrawerResize(event: PointerEvent): void {
    if (event.button !== 0 && event.pointerType !== "touch") return;
    event.preventDefault();
    resizingDrawer = true;
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    resizeDrawerFrom(event);
  }

  function moveDrawerResize(event: PointerEvent): void {
    if (resizingDrawer) resizeDrawerFrom(event);
  }

  function stopDrawerResize(event: PointerEvent): void {
    resizingDrawer = false;
    const handle = event.currentTarget as HTMLElement;
    if (handle.hasPointerCapture(event.pointerId)) {
      handle.releasePointerCapture(event.pointerId);
    }
  }

  /**
   * Showing something, which is not the same as holding something: dragged to the
   * floor the drawer keeps its panes at zero height. The control on the transport
   * is the only thing left saying whether the drawer is there, so it has to track
   * what a reader can actually see rather than what the stack contains.
   */
  const drawerOpen = $derived(drawer.groups.length > 0 && drawerHeight > 0);

  /* Plain fields, not `$state`: bookkeeping about whether a fit is owed, which
     nothing on screen reads and no fit should re-trigger. */
  let drawerSized = false;
  let drawerFitTimer: number | null = null;
  /* Long enough for the rows to be in the DOM after the events that made them —
     a warm load delivers a whole trace faster than it renders one, so the fit
     taken on the last event can still be a fit taken on half a trace. */
  const DRAWER_FIT_SETTLE_MS = 400;
  const DRAWER_FIT_TRIES = 20;

  /**
   * Fit when the drawer opens, and then leave it alone.
   *
   * A live run adds turns while the reader is reading. A drawer that grew with them
   * would walk the transport up the screen under the cursor mid-scrub, which is a
   * worse thing to do to someone than leave a band of empty space below the last
   * turn — so the fit happens once per open and the slack above absorbs the rest.
   * Collapsing or expanding a turn is the same argument: the reader asked to see a
   * turn, not to have the desk resize under them, and `.turns` scrolls.
   *
   * Asked once per open, and once per session, because those are the two moments the
   * question is new: a drawer being opened has no height yet, and a different run is
   * a different trace. A turn arriving in the run already on screen is not either of
   * those, which is why `run.total` is deliberately not read here — subscribing to it
   * made the drawer climb a row every turn until it had taken the rail, and a height
   * that grows while you watch is not a height anyone chose. `settleDrawerFit` is
   * what keeps that from meaning "measure once, too early".
   */
  $effect(() => {
    const holding = drawer.groups.length > 0;
    void run.session?.workflow_id;

    if (!holding) {
      /* An emptied drawer has no height anyone chose. The next open is a fresh
         one, and fresh means fitted. */
      drawerSized = false;
      return;
    }
    if (drawerSized) return;

    /* On the next frame, so a drawer opened onto a loaded session is never seen at
       the wrong size, and then until the measurement holds still. */
    requestAnimationFrame(() => {
      if (!drawerSized) fitDrawerToContent();
    });
    settleDrawerFit(null);
    return () => {
      if (drawerFitTimer != null) clearTimeout(drawerFitTimer);
      drawerFitTimer = null;
    };
  });

  /* The drawer's whole chrome, now that the pane header down there is hidden. One
     press always does the visible thing, which is why the two ways of being shut
     are answered differently: emptied, it needs the pane the drawer exists for —
     a waterfall is a wide, short thing and the rail's columns are the wrong shape
     for it — and dragged to the floor it only needs its height back. */
  function toggleDrawer(): void {
    if (drawerOpen) {
      /* Every pane, not just the latency one: the button says "close the drawer"
         and a reader who split a second view in beside it means that too. Pinned
         panes decline, which leaves the drawer open and the button pressed —
         still an honest reading of what is on screen. */
      for (const pane of drawer.groups.flat()) drawer.closePane(pane.id);
      return;
    }
    if (drawerHeight === 0) {
      /* Dragging to the floor asks for the drawer to be gone, not for it to be
         that tall next time, so reopening is a fresh open and gets a fresh fit.
         The fixed height is what it opens at while the fit has nothing to measure,
         and what it keeps if it never does. */
      drawerHeight = DRAWER_DEFAULT_H;
      drawerSized = false;
      requestAnimationFrame(() => fitDrawerToContent());
    }
    if (drawer.groups.length === 0) drawer.openPane({ kind: "latency" });
  }
</script>

<svelte:window
  onkeydown={handleWindowKeydown}
  onfocusin={noteRail}
  onpointerdown={noteRail}
/>

<main
  class="app"
  class:bleed={bleeding}
  class:bleed-drawer={drawer.bleedingPane != null}
  class:has-drawer={drawer.groups.length > 0}
  class:drawer-shut={drawerHeight === 0}
  class:drawer-solo={drawer.groups.length === 1 && drawer.groups[0].length === 1}
  style={`--drawer-h: ${drawerHeight}px`}
>
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
        <!-- The drawer's switch used to stand here. It moved onto the transport,
             which is the drawer's own top edge and the one strip still on screen
             when the drawer is bled — up here it was hidden at exactly the moment
             a reader wanted out. Two controls for one box was also the clutter
             this pass set out to remove. -->
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
    {paneContent}
  />
  <!-- Hoisted out of the rail it used to be declared in, unchanged: it was always
       parameterised by `pane`, so both rails render from this one body. -->
  {#snippet paneContent(pane: Pane)}
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
      {:else if pane.kind === "usage"}
        <!-- The same reading the transport's token chip used to open, now a pane
             that stays put while you scrub. The registry has described this kind
             since before anything rendered it; this is the body it was waiting for. -->
        <UsageReading
          usage={run.usage}
          usageTimeline={run.usageTimeline}
          viewIndex={run.viewIndex}
          unmeasured={run.runUnmeasured}
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

  <!-- Above the drawer, not below it: the transport is the drawer's top edge, so the
       cursor sits directly over the traces it moves and the playhead in every row
       reads as the same cursor. It stays a row of `.app` rather than a child of the
       drawer, which is what keeps it alive — and last — when the drawer is closed,
       and on screen when a drawer pane is bled. -->
  <StepController
    viewIndex={run.viewIndex}
    total={run.total}
    playing={run.playing}
    following={run.following}
    playbackSpeed={run.playbackSpeed}
    currentEvent={run.currentLogRow}
    turnMarkers={run.turnMarkers}
    anomalyMarkers={run.anomalyMarkers}
    eventRows={run.fullReplayLog.rows}
    {drawerOpen}
    onToggleDrawer={toggleDrawer}
    onPlay={() => run.play()}
    onPause={() => run.pause()}
    onSpeedChange={(speed) => run.setPlaybackSpeed(speed)}
    onJumpToLive={() => run.jumpToLive()}
    onReset={() => run.reset()}
    onScrub={(index) => run.goTo(index)}
  />

  <!-- The second rail. Same component, same snippets — the pane content is already
       parameterised by `pane`, so the drawer renders whatever the rail above can.
       Only rendered when it holds something, so the transport falls back against the
       rail the moment the last drawer pane is closed. -->
  {#if drawer.groups.length > 0}
    <section class="drawer" bind:this={drawerElement} aria-label="Bottom drawer">
      <button
        type="button"
        class="drawer-gutter"
        aria-label="Resize the bottom drawer"
        title="Drag to set the drawer height — double-click to fit it to the trace"
        onpointerdown={startDrawerResize}
        onpointermove={moveDrawerResize}
        onpointerup={stopDrawerResize}
        onpointercancel={stopDrawerResize}
        ondblclick={() => {
          /* The way back from a height you chose and no longer want, and the only way
             to ask the question again once it has settled. */
          drawerSized = false;
          if (fitDrawerToContent() == null) drawerHeight = DRAWER_DEFAULT_H;
        }}
      ></button>

      <PaneRail
        bind:this={drawerRail}
        stack={drawer}
        describe={describePane}
        bleedingId={drawer.bleedingPane?.id ?? null}
        {paneContent}
      />
    </section>
  {/if}
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

  /* The drawer opens under the transport and takes its height off the RAIL, which is
     what `minmax(0, 1fr)` on that row buys: the one strip that says where in the
     run you are looking from never gives ground, and the columns above absorb it.
     Capped, because a drawer that can eat the desk is a second desk.

     The transport being the row above is also the collapse behaviour: as the drawer
     gives up its height the trace is what goes, and the transport is the floor left
     standing — no collapsed state to model, it is just the row order. */
  .app.has-drawer {
    grid-template-rows: auto minmax(0, 1fr) auto min(60vh, var(--drawer-h));
  }

  /* One canvas means one canvas — but either rail may be holding it. `bleeding`
     is a single id per stack, so whichever stack has one takes the screen and the
     other rail stands down with the rest of the chrome. The waterfall is the pane
     most worth the whole width and it lives down here, so gating this to the rail
     above would have withheld it from exactly the pane that wanted it. */
  .app.bleed:not(.bleed-drawer) .drawer {
    display: none;
  }

  /* Direct child, so this reaches the rail above and never the one inside the
     drawer, which is the thing being shown. */
  .app.bleed-drawer > :global(.rail) {
    display: none;
  }

  /* The drawer is the 1fr row now, not a strip pinned above the transport. */
  .app.bleed-drawer .drawer {
    border-top: 0;
  }

  .app.bleed-drawer .drawer-gutter {
    display: none;
  }

  /* A grid of one row rather than a flex column: the rail is height-agnostic and
     takes whatever box its parent gives it, and a flex child with no `flex` is given
     nothing — the panes still painted, outside every ancestor that was supposed to
     clip and hit-test them, so nothing in the drawer could be pointed at. The gutter
     is absolutely positioned and takes no row of its own. */
  .drawer {
    position: relative;
    display: grid;
    grid-template-rows: minmax(0, 1fr);
    min-height: 0;
    border-top: 1px solid var(--border);
  }

  /* A drawer is one wide box, not a rail that carries on off to the right, so the
     column at the end of it takes whatever width is left. Nothing in the registry
     is marked flexible but the canvases — the waterfall never had a host this shape
     to be flexible in, and this is that host rather than a new fact about the kind. */
  .drawer :global(.rail-slot:last-child:not(.collapsed)) {
    flex: 1 1 var(--slot-size);
  }

  /* --- the drawer as an instrument ---------------------------------------------
     A trace is read for precision points in time, so in here everything that is not
     one gets out of the way and the scroller takes the height back. Scoped to the
     rail rather than asked of the box's shape: `F` bleeds this same drawer to the
     whole screen, and a threshold on size would have handed the title and the
     aggregates back at exactly the moment a reader asked for more trace. One
     instrument at two sizes, not two instruments.

     A rail column keeps the full form, which is what the request wanted — and is
     just as well, because it is the only place the model/tool/approval split is
     stated anywhere in the app. */
  .drawer :global(.waterfall) {
    grid-template-rows: minmax(0, 1fr);
  }

  /* The title says what the pane badge two lines above it already says, and the
     rollup is three run-wide totals — the stale reading, averaged over everything,
     that a waterfall is opened to get away from. */
  .drawer :global(.waterfall-head) {
    display: none;
  }

  .drawer :global(.turns) {
    padding-top: 4px;
  }

  /* The label column is sized for a rail, where 240px is the pane's own left half.
     Down here it holds `Turn 2` and `8m 35s` and the rest is air — and air to the
     left of a trace is the noise this drawer exists to be free of. Stacking the two
     strings cuts the column to the width of the longer one and hands the rest
     straight to the track.

     Fixed rather than `max-content`: every row is its own grid, so content-sizing
     would give each row a different left edge, the tracks would start at different
     x, and the sticky note above would line up with none of them. */
  .drawer :global(.turn-row) {
    grid-template-columns: 68px minmax(0, 1fr);
  }

  /* Descendant selectors rather than bare classes: these tie with the component's
     own rules on specificity, and a tie is decided by stylesheet order — which held
     for the row but not for the label, so `Turn 2` and `8m 35s` stayed side by side
     and both broke across lines instead. Stacked, each string has the column to
     itself. */
  .drawer :global(.turn-row .turn-label) {
    grid-template-columns: minmax(0, 1fr);
  }

  .drawer :global(.turn-row .turn-dur) {
    justify-self: start;
  }

  /* Pin and collapse are the shell's chrome, and neither means anything down here:
     the drawer is one box a reader opens and shuts, not a rail of columns to spine
     away, and pinning guards against a carry-over rule the drawer has no equivalent
     of. Close stays, because a pane you cannot shut is a trap, and so does the
     arrangement toggle, because the drawer really does hold tabs and splits.
     Reached by what the two buttons already carry — pin is the only pressed control
     in the header and collapse names itself — so PaneShell stays untouched. */
  .drawer :global(.head-button[aria-pressed]),
  .drawer :global(.head-button[aria-label^="Collapse"]) {
    display: none;
  }

  /* Alone in the drawer, a pane needs no header at all: the transport's own drawer
     button now shuts it, so the last thing the bar was carrying moved out, and a
     strip whose remaining job is to say LATENCY above a trace that is visibly a
     latency trace is a row of pixels charged to the scroller.

     Only when it is the single pane, which is the whole of the condition. Hiding
     the header costs the pane's close button, its drag handle and the arrangement
     toggle, and all three are things a reader only wants once there is a second
     pane to close, move or tab — at which point this stops matching and every one
     of them comes back. The one real loss is a second pane's own close, and that is
     exactly the case this does not fire in. `Cmd+Shift+Arrows` still moves panes:
     the header only advertised the shortcut, the window dispatcher owns it.

     PaneRail already hides the same header for a bled slot, so this is the pattern
     the rail set rather than a new one — and PaneShell stays untouched, five passes
     running. */
  .app.drawer-solo .drawer :global(.pane-head) {
    display: none;
  }

  /* Shut, but still holding its panes. The row is already zero — `min(60vh, 0px)` —
     so the only thing left to do is stop what no longer fits from painting outside
     the box meant to clip it. The header and the scale note go with everything else,
     which is also why they come straight back: there is no rule about either of them
     to get the wrong way round.

     On the rail and not on `.drawer`, which is the obvious place and the wrong one:
     `.drawer` is the gutter's containing block, so clipping there clips the handle
     too — and the handle sits above the drawer's zero-height box, so all of it. That
     left the drawer shut with no way to open it.

     Keyed off shut rather than a height, because bleeding overrides the grid row and
     a threshold would have fought it. */
  .app.drawer-shut .drawer :global(.rail) {
    overflow: hidden;
  }

  /* The handle is the only way back, and half its usual reach is now below the floor
     of the window. Give it the pixels above the seam, where the drawer used to be.
     It ties the transport on `z-index` and wins on tree order. */
  .app.drawer-shut .drawer-gutter {
    inset: -11px 0 auto 0;
  }

  /* Same handle as a column's width gutter, a quarter turn round: invisible until
     pointed at, sitting astride the seam it moves. */
  .drawer-gutter {
    position: absolute;
    inset: -6px 0 auto 0;
    z-index: 5;
    height: 12px;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: row-resize;
    touch-action: none;
    transition: background var(--duration-fast) var(--ease-out);
  }

  .drawer-gutter:focus-visible {
    background: color-mix(in srgb, var(--accent) 30%, transparent);
    outline: 2px solid var(--focus-ring);
    outline-offset: -4px;
  }

  @media (hover: hover) and (pointer: fine) {
    .drawer-gutter:hover {
      background: color-mix(in srgb, var(--accent) 30%, transparent);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .drawer-gutter {
      transition: none;
    }
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

  /* A bled drawer leaves two rows in the grid, transport then drawer, and the two
     above are the wrong way round for them — `minmax(0, 1fr)` first would stretch
     the transport and leave the trace at its content height. Must stay after
     `.app.bleed`, which it ties with on specificity. */
  .app.bleed-drawer {
    grid-template-rows: auto minmax(0, 1fr);
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
    /* Queried below, so the narrow-column rules answer to the box the pane actually
       got rather than to the window. A pane in a 200px rail column and the same pane
       filling a bottom drawer are the same viewport and very different rooms. */
    container-type: inline-size;
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

  /* The one rule that was doing real harm unconditionally: every host, however
     wide, was forced into the waterfall's one-column form, so a turn's label sat
     above its track and the axis followed it there. The label column comes back
     the moment there is room for it — which in a bottom drawer there always is.
     Every row follows whatever the host imposes, and still does; they just now get
     told the truth about the room. */
  @container (max-width: 640px) {
    .pane-content :global(.turn-row) {
      grid-template-columns: minmax(0, 1fr);
      gap: 8px;
    }
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
