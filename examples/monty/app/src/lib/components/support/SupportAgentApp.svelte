<script lang="ts">
  import {
    ArrowUp,
    AlertTriangle,
    ChevronDown,
    BrainCircuit,
    CheckCircle2,
    Clock3,
    Cpu,
    History,
    MessageCircle,
    Plus,
    Search,
    ShieldCheck,
    Sparkles,
    Trash2,
    XCircle,
    Wrench
  } from "@lucide/svelte";
  import { fade } from "svelte/transition";
  import type { AgentDescriptor, FileCitationAnnotation, Session } from "$lib/api/types";
  import type { ReplayLogRow } from "$lib/state/replayLog";
  import type { TranscriptItem } from "$lib/state/transcript";
  import MarkdownMessage from "./MarkdownMessage.svelte";

  interface Props {
    items: TranscriptItem[];
    logs?: ReplayLogRow[];
    sessions?: Session[];
    agentLabel: string;
    sessionId: string;
    agents?: AgentDescriptor[];
    currentAgentWorkflowType?: string | null;
    connecting?: boolean;
    sending?: boolean;
    creatingSession?: boolean;
    error?: string | null;
    onSend?: (message: string) => void | Promise<void>;
    onNewSession?: (workflowType: string) => void | Promise<void>;
    onSelectSession?: (sessionId: string) => void | Promise<void>;
    onDeleteSession?: (sessionId: string) => void | Promise<void>;
    onApproveTool?: (toolId: string, approved: boolean) => void | Promise<void>;
  }

  interface SupportMessage {
    id: string;
    role: "user" | "assistant";
    turnNumber?: number;
    text: string;
    timestamp: number;
    citations: FileCitationAnnotation[];
  }

  let {
    items,
    logs = [],
    sessions = [],
    agentLabel,
    sessionId,
    agents = [],
    currentAgentWorkflowType = null,
    connecting = false,
    sending = false,
    creatingSession = false,
    error = null,
    onSend,
    onNewSession,
    onSelectSession,
    onDeleteSession,
    onApproveTool
  }: Props = $props();
  let draft = $state("");
  let localMessages = $state<SupportMessage[]>([]);
  let observedSessionId = $state<string | null>(null);
  let expandedActivityTurns = $state<number[]>([]);
  let observedActivitySessionId = $state<string | null>(null);
  let observedActivityOffsets = $state<Record<number, number>>({});
  let deletingSessionIds = $state<string[]>([]);
  let resolvingApprovalIds = $state<string[]>([]);
  let approvalErrors = $state<Record<string, string>>({});

  const qaStarterQuestions = [
    "When should I use Signals vs Updates?",
    "How do I roll out a new Worker safely?",
    "Why did my Workflow keep running after deploy?"
  ];
  const montyStarterQuestions = [
    "Find me a flight from Seattle to Austin next Friday.",
    "Plan a three-night trip to Chicago with a hotel near downtown.",
    "Book the cheapest complete flight and hotel option for my trip."
  ];

  const fixtureMessages = $derived(seedMessages(items));
  const messages = $derived([...fixtureMessages, ...localMessages]);
  const logsByTurn = $derived(groupLogsByTurn(logs));
  const resolvedApprovalToolIds = $derived(resolvedApprovalIds(logs));
  const sources = $derived(uniqueCitations(messages.flatMap((message) => message.citations)));
  const sessionItems = $derived(sortedSessions(sessions));
  const isMonty = $derived(currentAgentWorkflowType === "MontyDynamicAgent");
  const isMontyTravelAgent = $derived(
    currentAgentWorkflowType?.startsWith("Monty") ?? false
  );
  const starterQuestions = $derived(
    isMonty ? [] : isMontyTravelAgent ? montyStarterQuestions : qaStarterQuestions
  );
  const composerPlaceholder = $derived(
    isMonty ? "Send a Python script to Monty" : `Ask ${agentLabel}`
  );
  const canCreateSession = $derived(
    Boolean(onNewSession) && agents.length > 0 && !connecting && !sending && !creatingSession
  );
  const statusLabel = $derived(
    creatingSession
      ? "Starting"
      : connecting
        ? "Connecting"
        : sending
          ? "Thinking"
          : error
            ? "Needs attention"
            : "Available"
  );

  $effect(() => {
    if (observedSessionId === null) {
      observedSessionId = sessionId;
      return;
    }

    if (observedSessionId !== sessionId) {
      observedSessionId = sessionId;
      draft = "";
      localMessages = [];
      observedActivitySessionId = null;
      observedActivityOffsets = {};
    }
  });

  $effect(() => {
    const nextOffsets: Record<number, number> = {};
    for (const [turnNumber, rows] of logsByTurn) {
      const active = rows[rows.length - 1];
      if (active) nextOffsets[turnNumber] = active.offset;
    }
    observedActivitySessionId = sessionId;
    observedActivityOffsets = nextOffsets;
  });

  function seedMessages(transcriptItems: TranscriptItem[]): SupportMessage[] {
    const messages: SupportMessage[] = [];
    const emittedUsers = new Set<number>();

    for (const item of transcriptItems) {
      if (item.kind === "user" && !item.text.startsWith("/")) {
        emittedUsers.add(item.turnNumber);
        messages.push({
          id: `support-user-${item.turnNumber}`,
          role: "user",
          turnNumber: item.turnNumber,
          text: item.text,
          timestamp: item.timestamp,
          citations: []
        });
      }

      if (item.kind === "agent") {
        if (!emittedUsers.has(item.turnNumber)) continue;
        messages.push({
          id: `support-agent-${item.turnNumber}`,
          role: "assistant",
          turnNumber: item.turnNumber,
          text: item.text,
          timestamp: item.timestamp,
          citations: item.citations
        });
      }
    }

    return messages;
  }

  function showLogInApp(row: ReplayLogRow): boolean {
    if (row.turnNumber <= 0) return false;
    if (row.actor === "user") return false;
    return ![
      "turn_started",
      "turn_end",
      "message_queued",
      "reply_delta",
      "reply",
      "text_annotation"
    ].includes(row.event);
  }

  function groupLogsByTurn(rows: ReplayLogRow[]): Map<number, ReplayLogRow[]> {
    const grouped = new Map<number, ReplayLogRow[]>();
    for (const row of rows) {
      if (!showLogInApp(row)) continue;
      const current = grouped.get(row.turnNumber) ?? [];
      current.push(row);
      grouped.set(row.turnNumber, current);
    }
    return grouped;
  }

  function resolvedApprovalIds(rows: ReplayLogRow[]): Set<string> {
    const result = new Set<string>();
    for (const row of rows) {
      if (row.event === "tool_approval_resolved" && row.toolId) result.add(row.toolId);
    }
    return result;
  }

  function isApprovalPending(row: ReplayLogRow): boolean {
    return (
      row.event === "tool_approval_requested" &&
      row.toolId != null &&
      !resolvedApprovalToolIds.has(row.toolId)
    );
  }

  function isApprovalResolving(toolId: string | undefined): boolean {
    return toolId != null && resolvingApprovalIds.includes(toolId);
  }

  function approvalError(toolId: string | undefined): string | null {
    return toolId ? approvalErrors[toolId] ?? null : null;
  }

  function logsForTurn(turnNumber: number | undefined): ReplayLogRow[] {
    if (turnNumber == null) return [];
    return logsByTurn.get(turnNumber) ?? [];
  }

  function activeLogForTurn(turnNumber: number | undefined): ReplayLogRow | null {
    const rows = logsForTurn(turnNumber);
    return rows[rows.length - 1] ?? null;
  }

  function activeLogFadeDuration(
    turnNumber: number | undefined,
    activeLog: ReplayLogRow | null
  ): number {
    if (turnNumber == null || activeLog == null) return 0;
    if (observedActivitySessionId !== sessionId) return 0;
    const observedOffset = observedActivityOffsets[turnNumber];
    return observedOffset != null && observedOffset !== activeLog.offset ? 150 : 0;
  }

  function activityExpanded(turnNumber: number | undefined): boolean {
    return turnNumber != null && expandedActivityTurns.includes(turnNumber);
  }

  function toggleActivity(turnNumber: number | undefined): void {
    if (turnNumber == null) return;
    expandedActivityTurns = activityExpanded(turnNumber)
      ? expandedActivityTurns.filter((item) => item !== turnNumber)
      : [...expandedActivityTurns, turnNumber];
  }

  function logTone(row: ReplayLogRow): string {
    if (row.tone === "error" || row.actor === "error") return "error";
    if (row.actor === "model") return "model";
    if (row.actor === "reasoning") return "reasoning";
    if (row.actor === "tool") return "tool";
    if (row.actor === "approval") return "approval";
    if (row.actor === "subagent") return "agent";
    if (row.tone === "done") return "done";
    return "neutral";
  }

  function activityLineClass(row: ReplayLogRow, active = false): string {
    return `activity-line ${logTone(row)}${active ? " active" : ""}`;
  }

  function logDetail(row: ReplayLogRow): string {
    const value = row.body ?? row.status ?? row.output ?? "";
    return value.split(/\r?\n/)[0]?.trim() ?? "";
  }

  async function resolveApproval(
    event: MouseEvent,
    row: ReplayLogRow,
    approved: boolean
  ): Promise<void> {
    event.stopPropagation();
    const toolId = row.toolId;
    if (!toolId || !onApproveTool || isApprovalResolving(toolId)) return;

    resolvingApprovalIds = [...resolvingApprovalIds, toolId];
    approvalErrors = { ...approvalErrors, [toolId]: "" };
    try {
      await onApproveTool(toolId, approved);
    } catch (error) {
      approvalErrors = {
        ...approvalErrors,
        [toolId]: error instanceof Error ? error.message : "Approval request failed."
      };
    } finally {
      resolvingApprovalIds = resolvingApprovalIds.filter((item) => item !== toolId);
    }
  }

  function time(value: number): string {
    return new Date(value * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
  }

  function sortedSessions(value: Session[]): Session[] {
    return [...value].sort((a, b) => b.created_at - a.created_at);
  }

  function sessionCreatedAt(value: number): string {
    if (!value) return "Unknown time";
    return new Date(value * 1000).toLocaleString([], {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    });
  }

  function sessionInitialMessage(session: Session): string {
    return session.initial_user_message?.trim() || "No user message yet";
  }

  function sessionAgentLabel(session: Session): string {
    return (
      agents.find((agent) => agent.workflow_type === session.agent_workflow_type)?.label ??
      session.agent_workflow_type
    );
  }

  function citationUrl(citation: FileCitationAnnotation): string {
    return citation.custom_metadata?.deep_url ?? citation.document_uri ?? "#";
  }

  function citationTitle(citation: FileCitationAnnotation): string {
    return (
      citation.custom_metadata?.heading ??
      citation.custom_metadata?.title ??
      citation.file_name ??
      "Source"
    );
  }

  function uniqueCitations(citations: FileCitationAnnotation[]): FileCitationAnnotation[] {
    const seen = new Set<string>();
    const result: FileCitationAnnotation[] = [];
    for (const citation of citations) {
      const key = citationUrl(citation);
      if (seen.has(key)) continue;
      seen.add(key);
      result.push(citation);
    }
    return result;
  }

  function responseFor(question: string): string {
    const normalized = question.toLowerCase();
    if (normalized.includes("worker") || normalized.includes("deploy")) {
      return "Use Worker Deployments when you need a controlled rollout. Start the new Worker pool, route a small slice of traffic, watch failures and latency, then ramp while old Workers drain existing executions.";
    }
    if (
      normalized.includes("signal") ||
      normalized.includes("update") ||
      normalized.includes("query")
    ) {
      return "Use a Signal for fire-and-forget input, an Update when the caller needs accepted or rejected semantics, and a Query for read-only status. Keep those primitives behind stable HTTP actions in the app.";
    }
    return "I found the closest matching Temporal guidance and would start with the message-passing and Worker rollout docs. If the issue is about an active execution, include the Workflow ID and the exact event you expected next.";
  }

  function suggestedCitations(question: string): FileCitationAnnotation[] {
    const normalized = question.toLowerCase();
    const workerSource = sources.find((source) =>
      citationTitle(source).toLowerCase().includes("worker")
    );
    const messageSource = sources.find((source) =>
      citationTitle(source).toLowerCase().includes("message")
    );
    if ((normalized.includes("worker") || normalized.includes("deploy")) && workerSource) {
      return [workerSource];
    }
    if (
      (normalized.includes("signal") ||
        normalized.includes("update") ||
        normalized.includes("query")) &&
      messageSource
    ) {
      return [messageSource];
    }
    return sources.slice(0, 2);
  }

  async function sendMessage(text = draft): Promise<void> {
    const question = text.trim();
    if (!question || sending || connecting || creatingSession) return;

    draft = "";
    if (onSend) {
      await onSend(question);
      return;
    }

    const now = Date.now() / 1000;
    const citations = suggestedCitations(question);
    localMessages = [
      ...localMessages,
      {
        id: `local-user-${now}`,
        role: "user",
        text: question,
        timestamp: now,
        citations: []
      },
      {
        id: `local-assistant-${now}`,
        role: "assistant",
        text: responseFor(question),
        timestamp: now + 1,
        citations
      }
    ];
  }

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void sendMessage();
  }

  async function startNewSession(workflowType: string): Promise<void> {
    if (!onNewSession || connecting || sending || creatingSession) return;
    await onNewSession(workflowType);
  }

  async function handleNewSessionAgentChange(event: Event): Promise<void> {
    const select = event.currentTarget as HTMLSelectElement;
    const workflowType = select.value;
    select.value = "";
    if (!workflowType) return;
    await startNewSession(workflowType);
  }

  async function selectSession(nextSessionId: string): Promise<void> {
    if (!onSelectSession || nextSessionId === sessionId) return;
    await onSelectSession(nextSessionId);
  }

  function sessionDeleting(nextSessionId: string): boolean {
    return deletingSessionIds.includes(nextSessionId);
  }

  async function deleteSession(nextSessionId: string): Promise<void> {
    if (!onDeleteSession || sessionDeleting(nextSessionId)) return;
    deletingSessionIds = [...deletingSessionIds, nextSessionId];
    try {
      await onDeleteSession(nextSessionId);
    } finally {
      deletingSessionIds = deletingSessionIds.filter((item) => item !== nextSessionId);
    }
  }
</script>

<section class="support-app" aria-label={`${agentLabel} customer chat`}>
  <div class="chat-shell">
    <header class="support-head">
      <div class="agent-mark" aria-hidden="true">
        <MessageCircle size={19} />
      </div>
      <div class="agent-title">
        <h2>{agentLabel}</h2>
        <p>{sessionId}</p>
      </div>
      <div class="agent-controls">
        <div class="agent-state">
          <span class={`live-dot ${error ? "error" : ""}`} aria-hidden="true"></span>
          <span>{statusLabel}</span>
        </div>

      </div>
    </header>

    <div class="message-list">
      {#if connecting && messages.length === 0}
        <div class="empty-chat">
          <Sparkles size={18} />
          <span>Connecting to {agentLabel}...</span>
        </div>
      {:else if error && messages.length === 0}
        <div class="empty-chat error">
          <span>{error}</span>
        </div>
      {/if}

      {#each messages as message}
        <article class={`message ${message.role}`}>
          {#if message.role === "assistant"}
            <div class="assistant-avatar" aria-hidden="true">
              <Sparkles size={15} />
            </div>
          {/if}

          <div class="bubble">
            <MarkdownMessage
              text={message.text}
              citations={message.role === "assistant" ? message.citations : []}
            />
          </div>
        </article>

        {#if message.role === "user"}
          {@const activityLogs = logsForTurn(message.turnNumber)}
          {@const activeLog = activeLogForTurn(message.turnNumber)}
          {@const expanded = activityExpanded(message.turnNumber)}
          {#if activityLogs.length > 0}
            <div class={`activity-feed ${expanded ? "expanded" : ""}`}>
              {#if expanded}
                <div class="activity-list">
                  {#each activityLogs as log}
                    <div class={activityLineClass(log, log.offset === activeLog?.offset)}>
                      <span class="activity-icon" aria-hidden="true">
                        {#if log.actor === "model"}
                          <Cpu size={14} />
                        {:else if log.actor === "reasoning"}
                          <BrainCircuit size={14} />
                        {:else if log.actor === "tool"}
                          <Wrench size={14} />
                        {:else if log.actor === "approval"}
                          <ShieldCheck size={14} />
                        {:else if log.actor === "subagent"}
                          <MessageCircle size={14} />
                        {:else if logTone(log) === "error"}
                          <AlertTriangle size={14} />
                        {:else if logTone(log) === "done"}
                          <CheckCircle2 size={14} />
                        {:else}
                          <Clock3 size={14} />
                        {/if}
                      </span>
                    <span class="activity-copy">
                      <strong>{log.label}</strong>
                      {#if logDetail(log)}
                        <span>{logDetail(log)}</span>
                      {/if}
                    </span>
                    <time>{time(log.timestamp)}</time>
                    {#if isApprovalPending(log)}
                      <div class="approval-actions">
                        <button
                          type="button"
                          class="approval-approve"
                          disabled={!onApproveTool || isApprovalResolving(log.toolId)}
                          onclick={(event) => void resolveApproval(event, log, true)}
                          onkeydown={(event) => event.stopPropagation()}
                        >
                          <CheckCircle2 size={13} />
                          <span>Approve</span>
                        </button>
                        <button
                          type="button"
                          class="approval-reject"
                          disabled={!onApproveTool || isApprovalResolving(log.toolId)}
                          onclick={(event) => void resolveApproval(event, log, false)}
                          onkeydown={(event) => event.stopPropagation()}
                        >
                          <XCircle size={13} />
                          <span>Reject</span>
                        </button>
                        {#if approvalError(log.toolId)}
                          <span class="approval-error">{approvalError(log.toolId)}</span>
                        {/if}
                      </div>
                    {/if}
                  </div>
                {/each}
              </div>
              {:else if activeLog}
                {#key activeLog.offset}
                  <div
                    class={activityLineClass(activeLog, true)}
                    in:fade={{ duration: activeLogFadeDuration(message.turnNumber, activeLog) }}
                  >
                    <span class="activity-icon" aria-hidden="true">
                      {#if activeLog.actor === "model"}
                        <Cpu size={14} />
                      {:else if activeLog.actor === "reasoning"}
                        <BrainCircuit size={14} />
                      {:else if activeLog.actor === "tool"}
                        <Wrench size={14} />
                      {:else if activeLog.actor === "approval"}
                        <ShieldCheck size={14} />
                      {:else if activeLog.actor === "subagent"}
                        <MessageCircle size={14} />
                      {:else if logTone(activeLog) === "error"}
                        <AlertTriangle size={14} />
                      {:else if logTone(activeLog) === "done"}
                        <CheckCircle2 size={14} />
                      {:else}
                        <Clock3 size={14} />
                      {/if}
                    </span>
                    <span class="activity-copy">
                      <strong>{activeLog.label}</strong>
                      {#if logDetail(activeLog)}
                        <span>{logDetail(activeLog)}</span>
                      {/if}
                    </span>
                    <time>{time(activeLog.timestamp)}</time>
                    {#if isApprovalPending(activeLog)}
                      <div class="approval-actions">
                        <button
                          type="button"
                          class="approval-approve"
                          disabled={!onApproveTool || isApprovalResolving(activeLog.toolId)}
                          onclick={(event) => void resolveApproval(event, activeLog, true)}
                          onkeydown={(event) => event.stopPropagation()}
                        >
                          <CheckCircle2 size={13} />
                          <span>Approve</span>
                        </button>
                        <button
                          type="button"
                          class="approval-reject"
                          disabled={!onApproveTool || isApprovalResolving(activeLog.toolId)}
                          onclick={(event) => void resolveApproval(event, activeLog, false)}
                          onkeydown={(event) => event.stopPropagation()}
                        >
                          <XCircle size={13} />
                          <span>Reject</span>
                        </button>
                        {#if approvalError(activeLog.toolId)}
                          <span class="approval-error">{approvalError(activeLog.toolId)}</span>
                        {/if}
                      </div>
                    {/if}
                  </div>
                {/key}
              {/if}
              <button
                type="button"
                class={`activity-expander ${expanded ? "expanded" : ""}`}
                aria-expanded={expanded}
                aria-label={expanded ? "Collapse activity logs" : "Expand activity logs"}
                onclick={() => toggleActivity(message.turnNumber)}
              >
                <ChevronDown size={14} />
              </button>
            </div>
          {/if}
        {/if}
      {/each}

      {#if sending}
        <article class="message assistant">
          <div class="assistant-avatar" aria-hidden="true">
            <Sparkles size={15} />
          </div>
          <div class="bubble thinking">
            <span></span><span></span><span></span>
          </div>
        </article>
      {/if}
    </div>

    {#if error && messages.length > 0}
      <div class="error-banner">{error}</div>
    {/if}

    {#if starterQuestions.length > 0}
      <div class="starter-row" aria-label="Suggested questions">
        {#each starterQuestions as question}
          <button
            type="button"
            disabled={sending || connecting || creatingSession}
            onclick={() => void sendMessage(question)}
          >
            {question}
          </button>
        {/each}
      </div>
    {/if}

    <form class="composer" onsubmit={handleSubmit}>
      <Search size={17} />
      <input
        bind:value={draft}
        placeholder={composerPlaceholder}
        aria-label={`Message ${agentLabel}`}
        disabled={connecting || creatingSession}
      />
      <button
        type="submit"
        aria-label="Send message"
        disabled={!draft.trim() || sending || connecting || creatingSession}
      >
        <ArrowUp size={17} />
      </button>
    </form>
  </div>

  <aside class="session-panel" aria-label="Sessions">
    <div class="session-head">
      <span class="session-title">
        <History size={16} />
        <span>Sessions</span>
      </span>
      <label class={`session-add-select ${canCreateSession ? "" : "disabled"}`}>
        <Plus size={14} aria-hidden="true" />
        <span class="session-add-label">{creatingSession ? "Starting" : "Add"}</span>
        <select
          aria-label="Add session"
          disabled={!canCreateSession}
          onchange={(event) => void handleNewSessionAgentChange(event)}
        >
          <option value="">{creatingSession ? "Starting" : "Add"}</option>
          {#each agents as agent}
            <option value={agent.workflow_type}>{agent.label}</option>
          {/each}
        </select>
        <ChevronDown size={13} aria-hidden="true" />
      </label>
    </div>

    <div class="session-list">
      {#if sessionItems.length === 0}
        <p class="session-empty">Sessions will appear after the app connects.</p>
      {/if}
      {#each sessionItems as item}
        <div
          class={`session-card ${item.workflow_id === sessionId ? "active" : ""}`}
          aria-current={item.workflow_id === sessionId ? "true" : undefined}
        >
          <button
            class="session-select"
            type="button"
            onclick={() => void selectSession(item.workflow_id)}
          >
            <span class="session-dot" aria-hidden="true"></span>
            <span class="session-copy">
              <time>{sessionCreatedAt(item.created_at)}</time>
              <strong>{sessionInitialMessage(item)}</strong>
              <small>{sessionAgentLabel(item)}</small>
            </span>
          </button>
          <button
            class="session-delete"
            type="button"
            aria-label={`Delete session ${sessionInitialMessage(item)}`}
            title="Delete session"
            disabled={!onDeleteSession || sessionDeleting(item.workflow_id)}
            onclick={() => void deleteSession(item.workflow_id)}
          >
            <Trash2 size={14} />
          </button>
        </div>
      {/each}
    </div>
  </aside>
</section>

<style>
  .support-app {
    width: 100%;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(280px, 340px);
    gap: 0;
    background: var(--surface-0);
  }

  .chat-shell {
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto auto;
    border-right: 1px solid var(--border);
  }

  .support-head {
    min-height: 66px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 18px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-1);
  }

  .agent-mark,
  .assistant-avatar {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    border-radius: 8px;
    background: color-mix(in srgb, var(--accent) 16%, var(--surface-2));
    color: var(--accent);
  }

  .agent-mark {
    width: 36px;
    height: 36px;
    border: 1px solid color-mix(in srgb, var(--accent) 32%, transparent);
  }

  .agent-title {
    min-width: 0;
    flex: 1 1 auto;
  }

  h2 {
    margin: 0;
    color: var(--text-1);
    font-size: 15px;
    line-height: 1.2;
  }

  .agent-title p {
    margin: 3px 0 0;
    overflow: hidden;
    color: var(--text-3);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-state {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--success);
    font-size: 12px;
    white-space: nowrap;
  }

  .agent-controls {
    min-width: 0;
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }

  .live-dot {
    width: 8px;
    height: 8px;
    border-radius: 999px;
    background: var(--success);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--success) 16%, transparent);
  }

  .live-dot.error {
    background: var(--error);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--error) 16%, transparent);
  }

  .message-list {
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 22px clamp(18px, 5vw, 72px);
  }

  .empty-chat {
    align-self: center;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    margin-top: 12vh;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text-2);
    background: var(--surface-1);
    font-size: 13px;
  }

  .empty-chat.error {
    color: var(--error);
    border-color: color-mix(in srgb, var(--error) 35%, var(--border));
  }

  .message {
    min-width: 0;
    display: flex;
    gap: 10px;
  }

  .message.user {
    justify-content: flex-end;
  }

  .message.assistant {
    justify-content: flex-start;
  }

  .assistant-avatar {
    width: 30px;
    height: 30px;
    margin-top: 2px;
  }

  .bubble {
    max-width: min(720px, 82%);
    min-width: 0;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-1);
    color: var(--text-1);
    line-height: 1.5;
  }

  .message.user .bubble {
    max-width: min(620px, 72%);
    border-color: color-mix(in srgb, var(--accent) 32%, transparent);
    background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
  }

  .activity-feed {
    position: relative;
    width: min(720px, 82%);
    display: grid;
    align-self: flex-start;
    margin-left: 40px;
    padding: 8px 34px 8px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: color-mix(in srgb, var(--surface-1) 78%, transparent);
    cursor: pointer;
    transition: border-color 160ms ease, background 160ms ease;
  }

  .activity-feed:hover,
  .activity-feed:focus-visible {
    border-color: var(--border-strong);
    background: color-mix(in srgb, var(--surface-2) 78%, transparent);
    outline: 0;
  }

  .activity-feed.expanded {
    gap: 8px;
  }

  .activity-list {
    display: grid;
    gap: 6px;
  }

  .activity-line {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 8px;
    align-items: center;
    color: var(--text-3);
    font-size: 12px;
  }

  .activity-line.active {
    color: var(--text-2);
  }

  .activity-icon {
    width: 22px;
    height: 22px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--surface-0);
  }

  .activity-line.active .activity-icon {
    border-color: color-mix(in srgb, currentColor 44%, var(--border));
    box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 12%, transparent);
  }

  .activity-line.model .activity-icon { color: var(--model); }
  .activity-line.reasoning .activity-icon { color: var(--reasoning); }
  .activity-line.tool .activity-icon { color: var(--warning); }
  .activity-line.approval .activity-icon { color: var(--queue); }
  .activity-line.done .activity-icon { color: var(--success); }
  .activity-line.error .activity-icon { color: var(--error); }

  .activity-copy {
    min-width: 0;
    display: inline-flex;
    gap: 7px;
    align-items: baseline;
    overflow: hidden;
    white-space: nowrap;
  }

  .activity-copy strong {
    flex: 0 0 auto;
    color: var(--text-2);
    font-weight: 650;
  }

  .activity-copy span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .activity-line time {
    color: var(--text-3);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .approval-actions {
    grid-column: 2 / 4;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    padding-top: 2px;
  }

  .approval-actions button {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    min-height: 26px;
    padding: 4px 8px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--surface-0);
    color: var(--text-2);
    cursor: pointer;
    font: inherit;
    font-size: 12px;
    line-height: 1;
  }

  .approval-actions button:hover:not(:disabled),
  .approval-actions button:focus-visible {
    border-color: var(--border-strong);
    outline: 0;
  }

  .approval-actions button:disabled {
    cursor: default;
    opacity: 0.55;
  }

  .approval-actions .approval-approve {
    color: var(--success);
    border-color: color-mix(in srgb, var(--success) 35%, var(--border));
  }

  .approval-actions .approval-reject {
    color: var(--error);
    border-color: color-mix(in srgb, var(--error) 35%, var(--border));
  }

  .approval-error {
    min-width: 0;
    color: var(--error);
    font-size: 11px;
  }

  .activity-expander {
    position: absolute;
    top: 12px;
    right: 10px;
    width: 18px;
    height: 18px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 0;
    border-radius: 5px;
    background: transparent;
    color: var(--text-3);
    cursor: pointer;
    transition: transform 160ms ease, color 160ms ease;
  }

  .activity-feed:hover .activity-expander,
  .activity-expander:focus-visible {
    color: var(--text-2);
    outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
    outline-offset: 2px;
  }

  .activity-expander.expanded {
    transform: rotate(180deg);
  }

  .thinking {
    display: inline-flex;
    gap: 5px;
    align-items: center;
    width: auto;
  }

  .thinking span {
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: var(--text-3);
    animation: pulse 900ms ease-in-out infinite;
  }

  .thinking span:nth-child(2) {
    animation-delay: 120ms;
  }

  .thinking span:nth-child(3) {
    animation-delay: 240ms;
  }

  @keyframes pulse {
    0%, 80%, 100% { opacity: 0.35; transform: translateY(0); }
    40% { opacity: 1; transform: translateY(-2px); }
  }

  .session-card:hover,
  .session-card:focus-within {
    border-color: var(--border-strong);
  }

  .starter-row {
    display: flex;
    gap: 8px;
    padding: 0 clamp(18px, 5vw, 72px) 10px;
    overflow-x: auto;
  }

  .starter-row button {
    flex: 0 0 auto;
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-1);
    color: var(--text-2);
    cursor: pointer;
    font: inherit;
    font-size: 12px;
  }

  .starter-row button:hover {
    color: var(--text-1);
    border-color: var(--border-strong);
  }

  .starter-row button:disabled {
    opacity: 0.45;
    cursor: default;
  }

  .error-banner {
    margin: 0 clamp(18px, 5vw, 72px) 10px;
    padding: 8px 10px;
    border: 1px solid color-mix(in srgb, var(--error) 35%, var(--border));
    border-radius: 7px;
    color: var(--error);
    background: color-mix(in srgb, var(--error) 9%, var(--surface-1));
    font-size: 12px;
  }

  .composer {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
    margin: 0 clamp(18px, 5vw, 72px) 18px;
    padding: 8px 8px 8px 12px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--surface-1);
    color: var(--text-3);
  }

  .composer input {
    min-width: 0;
    height: 32px;
    border: 0;
    outline: none;
    background: transparent;
    color: var(--text-1);
    font-size: 13px;
  }

  .composer input::placeholder {
    color: var(--text-3);
  }

  .composer input:disabled {
    opacity: 0.6;
  }

  .composer button {
    width: 32px;
    height: 32px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
    border-radius: 7px;
    background: color-mix(in srgb, var(--accent) 16%, var(--surface-2));
    color: var(--accent);
    cursor: pointer;
  }

  .composer button:disabled {
    opacity: 0.45;
    cursor: default;
  }

  .session-panel {
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    gap: 12px;
    padding: 16px;
    background: var(--surface-1);
  }

  .session-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    color: var(--text-2);
    font-size: 13px;
    font-weight: 650;
  }

  .session-title {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }

  .session-add-select {
    position: relative;
    height: 30px;
    display: inline-grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 6px;
    padding: 0 8px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-0);
    color: var(--text-2);
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
  }

  .session-add-label {
    min-width: 58px;
    max-width: 128px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .session-add-select:hover:not(.disabled),
  .session-add-select:focus-within:not(.disabled) {
    border-color: var(--border-strong);
    color: var(--text-1);
  }

  .session-add-select.disabled {
    opacity: 0.52;
    cursor: default;
  }

  .session-add-select select {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    border: 0;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    font-size: 12px;
    font-weight: 600;
    opacity: 0;
    outline: 0;
    appearance: none;
  }

  .session-add-select select:disabled {
    cursor: default;
  }

  .session-add-select option {
    background: var(--surface-1);
    color: var(--text-1);
  }

  .session-list {
    min-height: 0;
    overflow-y: auto;
    display: grid;
    align-content: start;
    gap: 8px;
  }

  .session-empty {
    margin: 0;
    color: var(--text-3);
    font-size: 12px;
    line-height: 1.4;
  }

  .session-card {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 6px;
    align-items: center;
    padding: 6px;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: inherit;
    background: var(--surface-2);
    transition: border-color 160ms ease, background 160ms ease;
  }

  .session-card.active {
    border-color: color-mix(in srgb, var(--accent) 44%, var(--border));
    background: color-mix(in srgb, var(--accent) 8%, var(--surface-2));
    cursor: default;
  }

  .session-select {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 9px;
    align-items: start;
    padding: 4px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .session-select:focus-visible,
  .session-delete:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--accent) 54%, transparent);
    outline-offset: 2px;
  }

  .session-dot {
    width: 8px;
    height: 8px;
    margin-top: 6px;
    border-radius: 999px;
    background: var(--text-3);
  }

  .session-card.active .session-dot {
    background: var(--accent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent);
  }

  .session-delete {
    width: 28px;
    height: 28px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid transparent;
    border-radius: 7px;
    background: transparent;
    color: var(--text-3);
    cursor: pointer;
  }

  .session-delete:hover:not(:disabled) {
    border-color: color-mix(in srgb, var(--error) 30%, var(--border));
    color: var(--error);
    background: color-mix(in srgb, var(--error) 9%, transparent);
  }

  .session-delete:disabled {
    opacity: 0.45;
    cursor: default;
  }

  .session-copy {
    min-width: 0;
    display: grid;
    gap: 4px;
  }

  .session-copy time {
    color: var(--text-1);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
  }

  .session-copy strong {
    display: -webkit-box;
    overflow: hidden;
    color: var(--text-2);
    font-size: 12px;
    line-height: 1.35;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  .session-copy small {
    overflow: hidden;
    color: var(--text-3);
    font-size: 11px;
    line-height: 1.35;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  @media (max-width: 760px) {
    .support-head {
      flex-wrap: wrap;
    }

    .agent-controls {
      width: 100%;
      margin-left: 48px;
      flex-wrap: wrap;
      justify-content: flex-start;
    }

  }

  @media (max-width: 980px) {
    .support-app {
      grid-template-columns: 1fr;
      grid-template-rows: minmax(0, 1fr) auto;
    }

    .chat-shell {
      border-right: 0;
    }

    .session-panel {
      max-height: 220px;
      border-top: 1px solid var(--border);
    }

    .message-list {
      padding-inline: 16px;
    }

    .bubble,
    .message.user .bubble {
      max-width: 88%;
    }

    .starter-row,
    .composer {
      margin-inline: 16px;
      padding-inline: 0;
    }

    .starter-row {
      padding-inline: 16px;
    }

    .composer {
      padding: 8px 8px 8px 12px;
    }
  }
</style>
