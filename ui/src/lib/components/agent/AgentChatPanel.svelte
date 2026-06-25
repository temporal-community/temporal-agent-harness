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
    X,
    XCircle,
    Wrench
  } from "@lucide/svelte";
  import { tick } from "svelte";
  import { fade } from "svelte/transition";
  import type {
    AgentDescriptor,
    AgentInboundMessage,
    AgentInterfaceFunction,
    FileCitationAnnotation,
    Session,
    SlashCommandApprovalMode,
    SlashCommandMessage,
    SlashCommandModel
  } from "$lib/api/types";
  import { formatCost } from "$lib/cost/pricing";
  import AgentGlyph from "$lib/components/primitives/AgentGlyph.svelte";
  import StatusChip, {
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";
  import type { ReplayLogRow } from "$lib/state/replayLog";
  import type { TranscriptItem } from "$lib/state/transcript";
  import MarkdownMessage from "$lib/components/chat/MarkdownMessage.svelte";

  type AgentChatLayout = "full" | "embedded";
  type SlashCommandId = "model" | "approvals" | "allow-tools" | "status";
  type SlashMenuItem =
    | { kind: "command"; id: SlashCommandId }
    | { kind: "model"; id: SlashCommandModel; model: SlashCommandModel }
    | { kind: "approval"; id: SlashCommandApprovalMode; mode: SlashCommandApprovalMode }
    | { kind: "tool"; id: string; tool: string };
  const slashCommandModels: SlashCommandModel[] = [
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite"
  ];
  const harnessSlashCommands: { id: SlashCommandId; label: string; detail: string }[] = [
    { id: "approvals", label: "/approvals", detail: "Set approval policy" },
    { id: "allow-tools", label: "/allow-tools", detail: "Auto-approve tools" },
    { id: "status", label: "/status", detail: "Show harness status" }
  ];
  const montySlashCommands: { id: SlashCommandId; label: string; detail: string }[] = [
    { id: "model", label: "/model", detail: "Set model" }
  ];
  const slashApprovalModes: SlashCommandApprovalMode[] = ["strict", "safe", "skip"];
  const montySlashAllowTools = [
    "run_monty_script",
    "search_flights",
    "search_hotels",
    "book_flight",
    "book_hotel",
    "get_trip_summary",
    "start_monty",
    "monty_run_script",
    "stop_monty"
  ];

  interface Props {
    items: TranscriptItem[];
    logs?: ReplayLogRow[];
    sessions?: Session[];
    agentLabel: string;
    sessionId: string;
    layout?: AgentChatLayout;
    showHeader?: boolean;
    agents?: AgentDescriptor[];
    agentInterface?: AgentInterfaceFunction[];
    currentAgentWorkflowType?: string | null;
    connecting?: boolean;
    sending?: boolean;
    creatingSession?: boolean;
    error?: string | null;
    onSend?: (message: AgentInboundMessage) => void | Promise<void>;
    onNewSession?: (workflowType: string) => void | Promise<void>;
    onSelectSession?: (sessionId: string) => void | Promise<void>;
    onDeleteSession?: (sessionId: string) => void | Promise<void>;
    onApproveTool?: (
      toolId: string,
      approved: boolean,
      remember?: boolean
    ) => void | Promise<void>;
  }

  interface ChatMessage {
    id: string;
    role: "user" | "assistant";
    turnNumber?: number;
    text: string;
    timestamp: number;
    citations: FileCitationAnnotation[];
  }

  interface TurnActivitySummary {
    label: string;
    detail: string;
    duration: string | null;
    endedAt: number;
  }

  let {
    items,
    logs = [],
    sessions = [],
    agentLabel,
    sessionId,
    layout = "full",
    showHeader = true,
    agents = [],
    agentInterface = [],
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
  let localMessages = $state<ChatMessage[]>([]);
  let observedSessionId = $state<string | null>(null);
  let sessionDrawerOpen = $state(false);
  let newSessionMenuOpen = $state(false);
  let sessionSearch = $state("");
  let expandedActivityTurns = $state<number[]>([]);
  let expandedLogRows = $state<string[]>([]);
  let observedActivitySessionId = $state<string | null>(null);
  let observedActivityOrdinals = $state<Record<number, number>>({});
  let deletingSessionIds = $state<string[]>([]);
  let resolvingApprovalIds = $state<string[]>([]);
  let approvalErrors = $state<Record<string, string>>({});
  let messageListElement = $state<HTMLDivElement | null>(null);
  let slashSelectionIndex = $state(0);
  let slashMenuSignature = $state("");

  const transcriptMessages = $derived(seedMessages(items));
  const messages = $derived([...transcriptMessages, ...localMessages]);
  const logsByTurn = $derived(groupLogsByTurn(logs));
  const resolvedApprovalToolIds = $derived(resolvedApprovalIds(logs));
  const pendingApprovalRows = $derived(logs.filter((row) => isApprovalPending(row)));
  const sources = $derived(uniqueCitations(messages.flatMap((message) => message.citations)));
  const sessionItems = $derived(sortedSessions(sessions));
  const sessionSearchTerm = $derived(sessionSearch.trim().toLowerCase());
  const filteredSessionItems = $derived(
    sessionSearchTerm
      ? sessionItems.filter((session) => sessionMatchesSearch(session, sessionSearchTerm))
      : sessionItems
  );
  const activeSession = $derived(
    sessionItems.find((item) => item.workflow_id === sessionId) ?? null
  );
  const activeAgent = $derived(
    agents.find((agent) => agent.workflow_type === activeSession?.agent_workflow_type) ??
      agents.find((agent) => agent.workflow_type === currentAgentWorkflowType) ??
      null
  );
  const supportsMontyRuntimeCommands = $derived(
    currentAgentWorkflowType === "MontyChatAgent" ||
      currentAgentWorkflowType === "MontyChatSubagentAgent"
  );
  const availableSlashCommands = $derived(
    supportsMontyRuntimeCommands
      ? [...harnessSlashCommands, ...montySlashCommands]
      : harnessSlashCommands
  );
  const acceptsSlashCommands = $derived(activeSession != null || agentInterface.length > 0);
  const isMonty = $derived(currentAgentWorkflowType === "MontyDynamicAgent");
  const composerPlaceholder = $derived(
    isMonty ? "Send a Python script to Monty" : `Ask ${agentLabel}`
  );
  const canCreateSession = $derived(
    Boolean(onNewSession) && agents.length > 0 && !creatingSession
  );
  const messageQueueingEnabled = $derived(
    activeSession?.is_message_queuing_enabled ?? false
  );
  const sendingBlocksInput = $derived(sending && !messageQueueingEnabled);
  const connectingBlocksInput = $derived(connecting && activeSession == null);
  const slashDraft = $derived(parseSlashDraft(draft));
  const slashMenuOpen = $derived(
    acceptsSlashCommands &&
      draft.trimStart().startsWith("/") &&
      !connectingBlocksInput &&
      !creatingSession
  );
  const slashModelChoices = $derived(filteredSlashModelChoices(slashDraft.arg));
  const slashApprovalChoices = $derived(filteredSlashApprovalChoices(slashDraft.arg));
  const slashToolSuggestions = $derived(uniqueToolSuggestions());
  const slashToolChoices = $derived(filteredSlashToolChoices(slashDraft.arg, slashToolSuggestions));
  const slashMenuItems = $derived(
    buildSlashMenuItems(
      slashMenuOpen,
      slashDraft,
      slashModelChoices,
      slashApprovalChoices,
      slashToolChoices
    )
  );
  const canSendDraft = $derived(
    Boolean(draft.trim()) &&
      !sendingBlocksInput &&
      !connectingBlocksInput &&
      !creatingSession &&
      (!draft.trimStart().startsWith("/") || slashMessageForDraft(draft) != null)
  );
  const drawerActive = $derived(showHeader && layout === "embedded" && sessionDrawerOpen);
  const latestMessage = $derived(messages[messages.length - 1] ?? null);
  const latestLog = $derived(logs[logs.length - 1] ?? null);
  const chatScrollSignature = $derived(
    [
      sessionId,
      drawerActive ? "drawer" : "chat",
      messages.length,
      latestMessage?.id ?? "",
      latestMessage?.text.length ?? 0,
      logs.length,
      latestLog?.ordinal ?? "",
      latestLog?.status ?? "",
      latestLog?.body?.length ?? 0,
      sending ? "sending" : "idle",
      connecting ? "connecting" : "connected",
      resolvingApprovalIds.length,
      Object.keys(approvalErrors).length
    ].join("|")
  );
  const statusLabel = $derived(
    creatingSession
      ? "Starting"
      : connecting
        ? "Connecting"
        : pendingApprovalRows.length > 0
          ? `${pendingApprovalRows.length} approval${
              pendingApprovalRows.length === 1 ? "" : "s"
            } needed`
          : sending
            ? "Thinking"
            : error
              ? "Needs attention"
              : "Available"
  );
  const statusKind = $derived(currentStatusKind());
  const statusDetail = $derived(
    error
      ? "intervention"
      : pendingApprovalRows.length > 0
        ? "human gate"
        : connecting
          ? "stream"
          : sending
            ? "turn active"
            : activeAgent?.label
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
      observedActivityOrdinals = {};
      expandedActivityTurns = [];
      expandedLogRows = [];
    }
  });

  $effect(() => {
    const nextOrdinals: Record<number, number> = {};
    for (const [turnNumber, rows] of logsByTurn) {
      const active = rows[rows.length - 1];
      if (active) nextOrdinals[turnNumber] = active.ordinal;
    }
    observedActivitySessionId = sessionId;
    observedActivityOrdinals = nextOrdinals;
  });

  $effect(() => {
    chatScrollSignature;
    if (drawerActive) return;

    void tick().then(() => {
      scrollMessagesToBottom();
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(scrollMessagesToBottom);
      }
    });
  });

  $effect(() => {
    const signature = slashMenuItems.map((item) => item.id).join("|");
    if (signature !== slashMenuSignature) {
      slashMenuSignature = signature;
      slashSelectionIndex = defaultSlashSelectionIndex(slashMenuItems);
      return;
    }

    if (slashMenuItems.length === 0) {
      slashSelectionIndex = 0;
    } else if (slashSelectionIndex >= slashMenuItems.length) {
      slashSelectionIndex = slashMenuItems.length - 1;
    }
  });

  function seedMessages(transcriptItems: TranscriptItem[]): ChatMessage[] {
    const messages: ChatMessage[] = [];
    const emittedUsers = new Set<number>();

    for (const item of transcriptItems) {
      if (item.kind === "user" && item.text.startsWith("/")) {
        emittedUsers.add(item.turnNumber);
      } else if (item.kind === "user") {
        emittedUsers.add(item.turnNumber);
        messages.push({
          id: `chat-user-${item.turnNumber}`,
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
          id: `chat-agent-${item.turnNumber}`,
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

  function allLogsForTurn(turnNumber: number | undefined): ReplayLogRow[] {
    if (turnNumber == null) return [];
    return logs.filter((row) => row.turnNumber === turnNumber && row.timestamp > 0);
  }

  function turnActivitySummary(
    turnNumber: number | undefined,
    visibleRows: ReplayLogRow[]
  ): TurnActivitySummary {
    const turnRows = allLogsForTurn(turnNumber);
    const rows = turnRows.length > 0 ? turnRows : visibleRows;
    const timestamps = rows.map((row) => row.timestamp).filter((value) => value > 0);
    const startedAt = Math.min(...timestamps);
    const endedAt = Math.max(...timestamps);
    const durationMs =
      timestamps.length >= 2 && Number.isFinite(startedAt) && Number.isFinite(endedAt)
        ? Math.max(0, (endedAt - startedAt) * 1000)
        : 0;

    return {
      label: turnNumber == null ? "Turn" : `Turn ${turnNumber}`,
      detail: `total ${formatCost(turnEstimatedCost(rows))}`,
      duration: durationMs > 0 ? formatElapsedDuration(durationMs) : null,
      endedAt: Number.isFinite(endedAt) ? endedAt : visibleRows[visibleRows.length - 1]?.timestamp ?? 0
    };
  }

  function turnEstimatedCost(rows: ReplayLogRow[]): number | null {
    const modelRows = rows.filter((row) => row.event === "model_interaction_ended");
    if (modelRows.some((row) => row.estimatedCostUsd == null)) return null;
    return modelRows.reduce((sum, row) => sum + (row.estimatedCostUsd ?? 0), 0);
  }

  function scrollMessagesToBottom(): void {
    const element = messageListElement;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  }

  function activeLogFadeDuration(
    turnNumber: number | undefined,
    activeLog: ReplayLogRow | null
  ): number {
    if (turnNumber == null || activeLog == null) return 0;
    if (observedActivitySessionId !== sessionId) return 0;
    const observedOrdinal = observedActivityOrdinals[turnNumber];
    return observedOrdinal != null && observedOrdinal !== activeLog.ordinal ? 150 : 0;
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

  function logExpanded(row: ReplayLogRow): boolean {
    return expandedLogRows.includes(row.id);
  }

  function toggleLog(row: ReplayLogRow): void {
    expandedLogRows = logExpanded(row)
      ? expandedLogRows.filter((item) => item !== row.id)
      : [...expandedLogRows, row.id];
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
    return [
      "activity-line",
      logTone(row),
      logHighlightClass(row),
      active ? "active" : ""
    ]
      .filter(Boolean)
      .join(" ");
  }

  function logHighlightClass(row: ReplayLogRow): string {
    if (row.tone === "error" || row.actor === "error") return "highlight-error";
    if (row.event === "tool_approval_resolved" && row.status === "approved") {
      return "highlight-success";
    }
    return "";
  }

  function logElapsedDuration(row: ReplayLogRow, rows: ReplayLogRow[]): string | null {
    const startRow = logDurationStart(row, rows) ?? previousLog(row, rows);
    if (!startRow) return null;

    const deltaMs = (row.timestamp - startRow.timestamp) * 1000;
    if (!Number.isFinite(deltaMs) || deltaMs <= 0) return null;
    return formatElapsedDuration(deltaMs);
  }

  function logDurationStart(row: ReplayLogRow, rows: ReplayLogRow[]): ReplayLogRow | null {
    if (row.event === "model_interaction_ended") {
      return previousLog(
        row,
        rows,
        (item) =>
          item.event === "model_interaction_started" &&
          sameLogScope(item, row) &&
          item.model === row.model
      );
    }

    if (row.event === "tool_end" || row.event === "tool_error") {
      return previousLog(
        row,
        rows,
        (item) =>
          item.event === "tool_start" &&
          sameLogScope(item, row) &&
          item.toolId === row.toolId
      );
    }

    if (row.event === "tool_approval_resolved") {
      return previousLog(
        row,
        rows,
        (item) =>
          item.event === "tool_approval_requested" &&
          sameLogScope(item, row) &&
          item.toolId === row.toolId
      );
    }

    if (row.event === "subagent_stopped") {
      return previousLog(
        row,
        rows,
        (item) =>
          item.event === "subagent_started" &&
          sameLogScope(item, row) &&
          item.detail === row.detail
      );
    }

    return null;
  }

  function previousLog(
    row: ReplayLogRow,
    rows: ReplayLogRow[],
    predicate: (item: ReplayLogRow) => boolean = () => true
  ): ReplayLogRow | null {
    const index = rows.findIndex((item) => item.id === row.id);
    for (let i = index - 1; i >= 0; i -= 1) {
      if (predicate(rows[i])) return rows[i];
    }
    return null;
  }

  function sameLogScope(a: ReplayLogRow, b: ReplayLogRow): boolean {
    return a.workflowId === b.workflowId && a.sourceTurnNumber === b.sourceTurnNumber;
  }

  function formatElapsedDuration(deltaMs: number): string {
    if (deltaMs < 1000) return `${Math.max(1, Math.round(deltaMs))}ms`;

    const seconds = deltaMs / 1000;
    const tenths = Math.round(seconds * 10) / 10;
    if (seconds < 10 && !Number.isInteger(tenths)) return `${tenths.toFixed(1)}s`;
    if (seconds < 60) return `${Math.round(seconds)}s`;

    const roundedSeconds = Math.round(seconds);
    return `${Math.floor(roundedSeconds / 60)}m ${String(roundedSeconds % 60).padStart(2, "0")}s`;
  }

  function logDetail(row: ReplayLogRow): string {
    const value = row.body ?? row.status ?? row.output ?? "";
    return value.split(/\r?\n/)[0]?.trim() ?? "";
  }

  function turnMessagePreview(value: string): string {
    return value.trim().replace(/\s+/g, " ") || "No user message";
  }

  function formatLogValue(value: unknown): string {
    if (value == null) return "";
    if (typeof value === "string") return value.trim();
    return JSON.stringify(value, null, 2);
  }

  function scriptPreview(script: string): string {
    const compact = script.trim().replace(/\s+/g, " ");
    return compact.length > 12 ? `${compact.slice(0, 12)}...` : compact;
  }

  function scriptFromValue(value: unknown): string | null {
    if (typeof value === "string") {
      try {
        return scriptFromValue(JSON.parse(value));
      } catch {
        return null;
      }
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        const script = scriptFromValue(item);
        if (script) return script;
      }
      return null;
    }
    if (typeof value !== "object" || value == null) return null;

    const record = value as Record<string, unknown>;
    if (typeof record.script === "string") return record.script;
    for (const item of Object.values(record)) {
      const script = scriptFromValue(item);
      if (script) return script;
    }
    return null;
  }

  function scrubScriptValue(value: unknown): unknown {
    if (Array.isArray(value)) return value.map((item) => scrubScriptValue(item));
    if (typeof value !== "object" || value == null) return value;

    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [
        key,
        key === "script" && typeof item === "string"
          ? scriptPreview(item)
          : scrubScriptValue(item)
      ])
    );
  }

  function logScript(row: ReplayLogRow): string | null {
    return (
      scriptFromValue(row.input) ??
      scriptFromValue(row.body) ??
      scriptFromValue(row.detail) ??
      scriptFromValue(row.output)
    );
  }

  function logFullDetail(row: ReplayLogRow): string {
    const sections: string[] = [];
    const body = formatLogValue(row.body);
    const detail = formatLogValue(row.detail);
    const input = formatLogValue(scrubScriptValue(row.input));
    const output = formatLogValue(row.output);
    const metadata = [
      `event: ${row.event}`,
      row.status ? `status: ${row.status}` : "",
      row.toolName ? `tool: ${row.toolName}` : "",
      row.toolId ? `tool_id: ${row.toolId}` : "",
      row.model ? `model: ${row.model}` : ""
    ].filter(Boolean);

    if (body) sections.push(body);
    if (detail) sections.push(`detail:\n${detail}`);
    if (input) sections.push(`input:\n${input}`);
    if (output) sections.push(`output:\n${output}`);
    if (metadata.length > 0) sections.push(metadata.join("\n"));

    return sections.join("\n\n");
  }

  async function resolveApproval(
    event: MouseEvent,
    row: ReplayLogRow,
    approved: boolean,
    remember = false
  ): Promise<void> {
    event.stopPropagation();
    const toolId = row.toolId;
    if (!toolId || !onApproveTool || isApprovalResolving(toolId)) return;

    resolvingApprovalIds = [...resolvingApprovalIds, toolId];
    approvalErrors = { ...approvalErrors, [toolId]: "" };
    try {
      await onApproveTool(toolId, approved, remember);
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

  function currentStatusKind(): StatusKind {
    if (error) return "error";
    if (pendingApprovalRows.length > 0) return "approval";
    if (creatingSession) return "starting";
    if (connecting) return "connecting";
    if (sending) return "thinking";
    return "available";
  }

  function sessionStatusKind(session: Session): StatusKind {
    if (session.workflow_id === sessionId) return statusKind;
    return session.is_message_queuing_enabled ? "queued" : "idle";
  }

  function sessionStatusLabel(session: Session): string {
    if (session.workflow_id === sessionId) return "Active";
    return session.is_message_queuing_enabled ? "Queue on" : "Idle";
  }

  function glyphStatusForSession(
    session: Session
  ): "available" | "busy" | "approval" | "error" | "idle" {
    if (session.workflow_id !== sessionId) return "idle";
    if (statusKind === "error") return "error";
    if (statusKind === "approval") return "approval";
    if (statusKind === "available" || statusKind === "complete") return "available";
    return "busy";
  }

  function sessionMatchesSearch(session: Session, term: string): boolean {
    return [
      sessionInitialMessage(session),
      sessionAgentLabel(session),
      session.workflow_id,
      session.agent_workflow_type
    ].some((value) => value.toLowerCase().includes(term));
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

  function parseSlashDraft(value: string): { command: string; arg: string } {
    const trimmed = value.trimStart();
    if (!trimmed.startsWith("/")) return { command: "", arg: "" };
    const withoutSlash = trimmed.slice(1);
    const [command = "", ...rest] = withoutSlash.split(/\s+/);
    return {
      command: command.toLowerCase(),
      arg: rest.join(" ").trim()
    };
  }

  function filteredSlashModelChoices(value: string): SlashCommandModel[] {
    const normalized = value.toLowerCase();
    if (!normalized) return slashCommandModels;
    return slashCommandModels.filter((model) =>
      model.toLowerCase().includes(normalized)
    );
  }

  function filteredSlashApprovalChoices(value: string): SlashCommandApprovalMode[] {
    const normalized = value.toLowerCase();
    if (!normalized) return slashApprovalModes;
    return slashApprovalModes.filter((mode) => mode.includes(normalized));
  }

  function uniqueToolSuggestions(): string[] {
    const pendingTools = pendingApprovalRows
      .map((row) => row.toolName)
      .filter((name): name is string => Boolean(name));
    const tools = supportsMontyRuntimeCommands
      ? [...pendingTools, ...montySlashAllowTools]
      : pendingTools;
    return [...new Set(tools)].sort((a, b) => a.localeCompare(b));
  }

  function filteredSlashToolChoices(value: string, tools: string[]): string[] {
    const normalized = value.toLowerCase();
    if (!normalized) return tools;
    return tools.filter((tool) => tool.toLowerCase().includes(normalized));
  }

  function slashCommandIds(): string[] {
    return availableSlashCommands.map((command) => command.id);
  }

  function canonicalSlashCommandId(command: string): SlashCommandId | null {
    const canonical = command === "allow-tool" ? "allow-tools" : command;
    return slashCommandIds().includes(canonical) ? (canonical as SlashCommandId) : null;
  }

  function buildSlashMenuItems(
    open: boolean,
    parsed: { command: string; arg: string },
    models: SlashCommandModel[],
    approvalModes: SlashCommandApprovalMode[],
    tools: string[]
  ): SlashMenuItem[] {
    if (!open) return [];
    const items: SlashMenuItem[] = [];
    const command = canonicalSlashCommandId(parsed.command);
    if (command == null) {
      items.push(
        ...availableSlashCommands
          .filter((command) => parsed.command === "" || command.id.startsWith(parsed.command))
          .map((command) => ({ kind: "command" as const, id: command.id }))
      );
    }
    if (command === "model") {
      items.push(
        ...models.map((model) => ({
          kind: "model" as const,
          id: model,
          model
        }))
      );
    }
    if (command === "approvals") {
      items.push(
        ...approvalModes.map((mode) => ({
          kind: "approval" as const,
          id: mode,
          mode
        }))
      );
    }
    if (command === "allow-tools") {
      items.push(
        ...tools.map((tool) => ({
          kind: "tool" as const,
          id: tool,
          tool
        }))
      );
    }
    return items;
  }

  function defaultSlashSelectionIndex(items: SlashMenuItem[]): number {
    const firstChoiceIndex = items.findIndex((item) => item.kind !== "command");
    return firstChoiceIndex === -1 ? 0 : firstChoiceIndex;
  }

  function slashMessageForDraft(value: string): SlashCommandMessage | null {
    const parsed = parseSlashDraft(value);
    const command = canonicalSlashCommandId(parsed.command);
    if (command == null) return null;
    if (command === "model") {
      const model = slashCommandModels.find((item) => item === parsed.arg);
      if (!model) return null;
      return { type: "slash", payload: { name: "set-model", arg: model } };
    }
    if (command === "approvals") {
      const mode = slashApprovalModes.find((item) => item === parsed.arg);
      if (!mode) return null;
      return { type: "slash", payload: { name: "set-approvals", arg: mode } };
    }
    if (command === "allow-tools") {
      if (!parsed.arg) return null;
      return { type: "slash", payload: { name: "allow-tools", arg: parsed.arg } };
    }
    if (command === "status" && !parsed.arg) {
      return { type: "slash", payload: { name: "status" } };
    }
    return null;
  }

  function commandDisplayText(message: AgentInboundMessage): string {
    if (typeof message === "string") return message;
    if (
      message.type === "slash" &&
      typeof message.payload === "object" &&
      message.payload != null &&
      "name" in message.payload &&
      typeof message.payload.name === "string"
    ) {
      const command = slashDisplayCommand(message.payload.name);
      const arg =
        "arg" in message.payload && typeof message.payload.arg === "string"
          ? message.payload.arg
          : "";
      return `/${command}${arg ? ` ${arg}` : ""}`;
    }
    return JSON.stringify(message);
  }

  function slashDisplayCommand(name: string): string {
    if (name === "set-model") return "model";
    if (name === "set-approvals") return "approvals";
    if (name === "allow-tool") return "allow-tools";
    return name;
  }

  async function sendModelCommand(model: SlashCommandModel): Promise<void> {
    await sendMessage(`/model ${model}`);
  }

  async function sendApprovalCommand(mode: SlashCommandApprovalMode): Promise<void> {
    await sendMessage(`/approvals ${mode}`);
  }

  async function sendAllowToolCommand(tool: string): Promise<void> {
    await sendMessage(`/allow-tools ${tool}`);
  }

  function selectSlashCommand(command: SlashCommandId): void {
    if (command === "status") {
      void sendMessage("/status");
      return;
    }
    draft = `/${command} `;
  }

  async function sendMessage(text = draft): Promise<void> {
    const question = text.trim();
    const slashMessage = slashMessageForDraft(question);
    if (
      !question ||
      sendingBlocksInput ||
      connectingBlocksInput ||
      creatingSession ||
      (question.startsWith("/") && slashMessage == null)
    ) {
      return;
    }
    const outbound: AgentInboundMessage = slashMessage ?? question;
    const displayText = commandDisplayText(outbound);

    draft = "";
    if (onSend) {
      await onSend(outbound);
      return;
    }

    const now = Date.now() / 1000;
    const citations = suggestedCitations(displayText);
    localMessages = [
      ...localMessages,
      {
        id: `local-user-${now}`,
        role: "user",
        text: displayText,
        timestamp: now,
        citations: []
      },
      {
        id: `local-assistant-${now}`,
        role: "assistant",
        text: responseFor(displayText),
        timestamp: now + 1,
        citations
      }
    ];
  }

  function selectedSlashMenuItem(): SlashMenuItem | null {
    return slashMenuItems[slashSelectionIndex] ?? slashMenuItems[0] ?? null;
  }

  function slashItemActive(item: SlashMenuItem): boolean {
    return selectedSlashMenuItem()?.id === item.id;
  }

  function acceptSlashSelection(): boolean {
    if (!slashMenuOpen) return false;
    const selected = selectedSlashMenuItem();
    if (!selected) return false;

    if (selected.kind === "model") {
      void sendModelCommand(selected.model);
      return true;
    }

    if (selected.kind === "approval") {
      void sendApprovalCommand(selected.mode);
      return true;
    }

    if (selected.kind === "tool") {
      void sendAllowToolCommand(selected.tool);
      return true;
    }

    if (selected.kind === "command") {
      if (selected.id === "status") {
        void sendMessage("/status");
        return true;
      }
      draft = `/${selected.id} `;
      return true;
    }

    return false;
  }

  function moveSlashSelection(delta: number): boolean {
    if (!slashMenuOpen || slashMenuItems.length === 0) return false;
    slashSelectionIndex =
      (slashSelectionIndex + delta + slashMenuItems.length) % slashMenuItems.length;
    return true;
  }

  function handleComposerKeydown(event: KeyboardEvent): void {
    if (event.altKey || event.ctrlKey || event.metaKey) return;

    if ((event.key === "ArrowDown" || event.key === "ArrowRight") && moveSlashSelection(1)) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }

    if ((event.key === "ArrowUp" || event.key === "ArrowLeft") && moveSlashSelection(-1)) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }

    if ((event.key === "Tab" && !event.shiftKey) || event.key === "Enter") {
      if (!acceptSlashSelection()) return;
      event.preventDefault();
      event.stopPropagation();
    }
  }

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void sendMessage();
  }

  async function startNewSession(workflowType: string): Promise<void> {
    if (!onNewSession || creatingSession) return;
    await onNewSession(workflowType);
    newSessionMenuOpen = false;
    sessionDrawerOpen = false;
  }

  function toggleNewSessionMenu(): void {
    if (!canCreateSession) return;
    newSessionMenuOpen = !newSessionMenuOpen;
    if (newSessionMenuOpen) sessionDrawerOpen = false;
  }

  function toggleSessionDrawer(): void {
    sessionDrawerOpen = !sessionDrawerOpen;
    if (sessionDrawerOpen) newSessionMenuOpen = false;
  }

  async function selectSession(nextSessionId: string): Promise<void> {
    if (!onSelectSession || nextSessionId === sessionId) return;
    await onSelectSession(nextSessionId);
  }

  async function openSession(nextSessionId: string): Promise<void> {
    await selectSession(nextSessionId);
    sessionDrawerOpen = false;
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

<section
  class={`agent-chat ${layout} ${showHeader ? "" : "headerless"}`}
  aria-label={`${agentLabel} customer chat`}
>
  <div class="chat-shell">
    {#if showHeader}
      <header class="agent-chat-head">
        <div class="agent-mark" aria-hidden="true">
          <AgentGlyph
            label={activeAgent?.label ?? agentLabel}
            workflowType={currentAgentWorkflowType}
            status={statusKind === "error"
              ? "error"
              : statusKind === "approval"
                ? "approval"
                : statusKind === "available"
                  ? "available"
                  : "busy"}
            size={layout === "embedded" ? "md" : "lg"}
          />
        </div>
        <div class="agent-title">
          <h2>{agentLabel}</h2>
          <p>{sessionId}</p>
        </div>
        <div class="agent-controls">
          {#if layout === "embedded"}
            <div class="new-session-control">
              <button
                type="button"
                class="header-session-add"
                class:disabled={!canCreateSession}
                class:active={newSessionMenuOpen}
                disabled={!canCreateSession}
                aria-haspopup="menu"
                aria-expanded={newSessionMenuOpen}
                onclick={toggleNewSessionMenu}
              >
                <Plus size={13} aria-hidden="true" />
                <span>{creatingSession ? "Starting" : "New"}</span>
                <span class="control-chevron" aria-hidden="true">
                  <ChevronDown size={13} />
                </span>
              </button>
              {#if newSessionMenuOpen}
                <section class="agent-command-menu header-menu" aria-label="New session">
                  {#each agents as agent}
                    <button
                      type="button"
                      class="agent-command-row"
                      onclick={() => void startNewSession(agent.workflow_type)}
                    >
                      <AgentGlyph
                        label={agent.label}
                        workflowType={agent.workflow_type}
                        status="available"
                      />
                      <span class="agent-command-copy">
                        <strong>{agent.label}</strong>
                        <small>{agent.description || agent.workflow_type}</small>
                      </span>
                      <StatusChip label="Ready" kind="available" compact />
                    </button>
                  {/each}
                </section>
              {/if}
            </div>
            <button
              type="button"
              class="header-session-drawer"
              class:active={sessionDrawerOpen}
              aria-pressed={sessionDrawerOpen}
              onclick={toggleSessionDrawer}
            >
              <History size={13} />
              <span>Sessions</span>
              <span class="control-chevron" aria-hidden="true">
                <ChevronDown size={13} />
              </span>
            </button>
          {/if}
          <StatusChip
            label={statusLabel}
            kind={statusKind}
            detail={statusDetail}
            active={statusKind === "thinking" || statusKind === "connecting"}
          />
        </div>
      </header>
    {/if}

    {#if drawerActive}
      <section class="session-drawer" aria-label="Sessions">
        <header class="session-drawer-head">
          <span class="session-drawer-title">
            <History size={15} />
            <span>Sessions</span>
          </span>
          <button
            type="button"
            class="session-drawer-close"
            aria-label="Close sessions"
            onclick={() => (sessionDrawerOpen = false)}
          >
            <X size={15} />
          </button>
        </header>

        <label class="session-drawer-search">
          <Search size={14} aria-hidden="true" />
          <input
            bind:value={sessionSearch}
            placeholder="Search sessions"
            aria-label="Search sessions"
          />
        </label>

        <div class="session-drawer-list">
          {#if filteredSessionItems.length === 0}
            <p class="session-empty">No matching sessions.</p>
          {/if}
          {#each filteredSessionItems as item}
            <button
              type="button"
              class={`drawer-session-row ${item.workflow_id === sessionId ? "active" : ""}`}
              aria-current={item.workflow_id === sessionId ? "true" : undefined}
              onclick={() => void openSession(item.workflow_id)}
            >
              <AgentGlyph
                label={sessionAgentLabel(item)}
                workflowType={item.agent_workflow_type}
                status={glyphStatusForSession(item)}
              />
              <span class="session-copy">
                <time>{sessionCreatedAt(item.created_at)}</time>
                <strong>{sessionInitialMessage(item)}</strong>
                <small>{sessionAgentLabel(item)}</small>
              </span>
              <StatusChip
                label={sessionStatusLabel(item)}
                kind={sessionStatusKind(item)}
                compact
                active={item.workflow_id === sessionId && statusKind !== "available"}
              />
            </button>
          {/each}
        </div>
      </section>
    {:else}
      <div class="message-list" bind:this={messageListElement}>
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
            {#if activityLogs.length > 0 && activeLog}
              {@const turnSummary = turnActivitySummary(message.turnNumber, activityLogs)}
              <div class={`activity-feed ${expanded ? "expanded" : ""}`}>
                {#key activeLog.ordinal}
                  <button
                    type="button"
                    class={`activity-summary ${expanded ? "expanded" : ""} activity-line turn-summary active`}
                    aria-expanded={expanded}
                    aria-label={expanded ? "Collapse activity logs" : "Expand activity logs"}
                    onclick={() => toggleActivity(message.turnNumber)}
                    in:fade={{ duration: activeLogFadeDuration(message.turnNumber, activeLog) }}
                  >
                    <span class="activity-icon" aria-hidden="true">
                      <History size={14} />
                    </span>
                    <span class="activity-copy">
                      <span class="activity-heading">
                        <strong>{turnSummary.label}</strong>
                        <span>{turnSummary.detail}</span>
                      </span>
                      <span class="activity-message">{turnMessagePreview(message.text)}</span>
                    </span>
                    <span
                      class="activity-duration"
                      aria-hidden={turnSummary.duration ? undefined : "true"}
                    >
                      {turnSummary.duration ?? ""}
                    </span>
                    <time>{time(turnSummary.endedAt)}</time>
                    <ChevronDown class="activity-chevron" size={14} aria-hidden="true" />
                  </button>
                {/key}

                {#if expanded}
                  <div class="activity-list">
                    {#each activityLogs as log}
                      {@const rowExpanded = logExpanded(log)}
                      {@const fullDetail = logFullDetail(log)}
                      {@const scriptDetail = logScript(log)}
                      {@const rowDuration = logElapsedDuration(log, activityLogs)}
                      <div class={`activity-row ${rowExpanded ? "expanded" : ""}`}>
                        <button
                          type="button"
                          class={`${activityLineClass(log, log.ordinal === activeLog.ordinal)} activity-row-button`}
                          aria-expanded={rowExpanded}
                          onclick={() => toggleLog(log)}
                        >
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
                          <span
                            class="activity-duration"
                            aria-hidden={rowDuration ? undefined : "true"}
                          >
                            {rowDuration ?? ""}
                          </span>
                          <time>{time(log.timestamp)}</time>
                          <ChevronDown class="activity-row-chevron" size={13} aria-hidden="true" />
                        </button>

                        {#if rowExpanded}
                          {#if scriptDetail}
                            <pre class="activity-script-detail" data-language="python"><code>{scriptDetail}</code></pre>
                          {/if}
                          <pre class="activity-detail">{fullDetail}</pre>
                        {/if}
                      </div>
                    {/each}
                  </div>
                {/if}
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
    {/if}

    {#if !drawerActive && error && messages.length > 0}
      <div class="error-banner">{error}</div>
    {/if}

    {#if !drawerActive && pendingApprovalRows.length > 0}
      <section class="pending-approvals" aria-label="Pending tool approvals">
        <header class="pending-approvals-head">
          <StatusChip
            label={`${pendingApprovalRows.length} approval${
              pendingApprovalRows.length === 1 ? "" : "s"
            } needed`}
            kind="approval"
            detail="human gate"
            active
          />
        </header>

        <div class="pending-approval-list">
          {#each pendingApprovalRows as approval}
            <article class="pending-approval-card">
              <div class="pending-approval-copy">
                <strong>{approval.toolName ?? approval.body ?? "Tool approval"}</strong>
                <span>Turn {approval.turnNumber} · {time(approval.timestamp)}</span>
                <StatusChip label="Awaiting approval" kind="approval" compact active />
              </div>
              <div class="approval-actions compact">
                <button
                  type="button"
                  class="approval-approve"
                  disabled={!onApproveTool || isApprovalResolving(approval.toolId)}
                  onclick={(event) => void resolveApproval(event, approval, true)}
                  onkeydown={(event) => event.stopPropagation()}
                >
                  <CheckCircle2 size={13} />
                  <span>Approve</span>
                </button>
                <button
                  type="button"
                  class="approval-remember"
                  disabled={!onApproveTool || isApprovalResolving(approval.toolId)}
                  onclick={(event) => void resolveApproval(event, approval, true, true)}
                  onkeydown={(event) => event.stopPropagation()}
                >
                  <ShieldCheck size={13} />
                  <span>Approve and remember</span>
                </button>
                <button
                  type="button"
                  class="approval-reject"
                  disabled={!onApproveTool || isApprovalResolving(approval.toolId)}
                  onclick={(event) => void resolveApproval(event, approval, false)}
                  onkeydown={(event) => event.stopPropagation()}
                >
                  <XCircle size={13} />
                  <span>Reject</span>
                </button>
                {#if approvalError(approval.toolId)}
                  <span class="approval-error">{approvalError(approval.toolId)}</span>
                {/if}
              </div>
            </article>
          {/each}
        </div>
      </section>
    {/if}

    <div class="composer-wrap">
      {#if slashMenuOpen}
        <section class="slash-menu" aria-label="Slash commands">
          {#each availableSlashCommands.filter((command) => slashDraft.command === "" || command.id.startsWith(slashDraft.command)) as command}
            {#if !slashCommandIds().includes(slashDraft.command)}
              {@const commandItem = { kind: "command", id: command.id } as const}
              <button
                type="button"
                class={`slash-row ${slashItemActive(commandItem) ? "active" : ""}`}
                onclick={() => selectSlashCommand(command.id)}
              >
                {#if command.id === "model"}
                  <BrainCircuit size={15} />
                {:else if command.id === "approvals"}
                  <ShieldCheck size={15} />
                {:else}
                  <Wrench size={15} />
                {/if}
                <span>
                  <strong>{command.label}</strong>
                  <small>{command.detail}</small>
                </span>
              </button>
            {/if}
          {/each}

          {#if slashCommandIds().includes(slashDraft.command)}
            {@const command = availableSlashCommands.find((item) => item.id === slashDraft.command)}
            {#if command}
              <div class="slash-row slash-command-summary">
                {#if command.id === "model"}
                  <BrainCircuit size={15} />
                {:else if command.id === "approvals"}
                  <ShieldCheck size={15} />
                {:else}
                  <Wrench size={15} />
                {/if}
                <span>
                  <strong>{command.label}</strong>
                  <small>{command.detail}</small>
                </span>
              </div>
            {/if}
          {/if}

          {#if slashDraft.command === "model"}
            <div class="slash-models" aria-label="Model choices">
              {#each slashModelChoices as model}
                {@const modelItem = { kind: "model", id: model, model } as const}
                <button
                  type="button"
                  class={`slash-row model-choice ${slashItemActive(modelItem) ? "active" : ""}`}
                  onclick={() => void sendModelCommand(model)}
                >
                  <Cpu size={15} />
                  <span>
                    <strong>{model}</strong>
                    <small>set-model</small>
                  </span>
                </button>
              {/each}
            </div>
          {/if}
          {#if slashDraft.command === "approvals"}
            <div class="slash-models" aria-label="Approval choices">
              {#each slashApprovalChoices as mode}
                {@const approvalItem = { kind: "approval", id: mode, mode } as const}
                <button
                  type="button"
                  class={`slash-row model-choice ${slashItemActive(approvalItem) ? "active" : ""}`}
                  onclick={() => void sendApprovalCommand(mode)}
                >
                  <ShieldCheck size={15} />
                  <span>
                    <strong>{mode}</strong>
                    <small>set-approvals</small>
                  </span>
                </button>
              {/each}
            </div>
          {/if}
          {#if slashDraft.command === "allow-tools" || slashDraft.command === "allow-tool"}
            <div class="slash-models" aria-label="Tool choices">
              {#each slashToolChoices as tool}
                {@const toolItem = { kind: "tool", id: tool, tool } as const}
                <button
                  type="button"
                  class={`slash-row model-choice ${slashItemActive(toolItem) ? "active" : ""}`}
                  onclick={() => void sendAllowToolCommand(tool)}
                >
                  <Wrench size={15} />
                  <span>
                    <strong>{tool}</strong>
                    <small>allow-tools</small>
                  </span>
                </button>
              {/each}
            </div>
          {/if}
        </section>
      {/if}

      <form class="composer" onsubmit={handleSubmit}>
        <Search size={17} />
        <input
          bind:value={draft}
          placeholder={composerPlaceholder}
          aria-label={`Message ${agentLabel}`}
          disabled={connectingBlocksInput || creatingSession}
          onkeydown={handleComposerKeydown}
        />
        <button
          type="submit"
          aria-label="Send message"
          disabled={!canSendDraft}
        >
          <ArrowUp size={17} />
        </button>
      </form>
    </div>
  </div>

  {#if layout === "full"}
    <aside class="session-panel" aria-label="Sessions">
      <div class="session-head">
        <span class="session-title">
          <History size={16} />
          <span>Sessions</span>
        </span>
        <div class="new-session-control panel-add">
          <button
            type="button"
            class="session-add-select"
            class:disabled={!canCreateSession}
            class:active={newSessionMenuOpen}
            disabled={!canCreateSession}
            aria-haspopup="menu"
            aria-expanded={newSessionMenuOpen}
            onclick={toggleNewSessionMenu}
          >
            <Plus size={14} aria-hidden="true" />
            <span class="session-add-label">{creatingSession ? "Starting" : "Add"}</span>
            <span class="control-chevron" aria-hidden="true">
              <ChevronDown size={13} />
            </span>
          </button>
          {#if newSessionMenuOpen}
            <section class="agent-command-menu panel-menu" aria-label="New session">
              {#each agents as agent}
                <button
                  type="button"
                  class="agent-command-row"
                  onclick={() => void startNewSession(agent.workflow_type)}
                >
                  <AgentGlyph
                    label={agent.label}
                    workflowType={agent.workflow_type}
                    status="available"
                  />
                  <span class="agent-command-copy">
                    <strong>{agent.label}</strong>
                    <small>{agent.description || agent.workflow_type}</small>
                  </span>
                  <StatusChip label="Ready" kind="available" compact />
                </button>
              {/each}
            </section>
          {/if}
        </div>
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
              <AgentGlyph
                label={sessionAgentLabel(item)}
                workflowType={item.agent_workflow_type}
                status={glyphStatusForSession(item)}
                size="sm"
              />
              <span class="session-copy">
                <time>{sessionCreatedAt(item.created_at)}</time>
                <strong>{sessionInitialMessage(item)}</strong>
                <small>{sessionAgentLabel(item)}</small>
              </span>
            </button>
            <StatusChip
              label={sessionStatusLabel(item)}
              kind={sessionStatusKind(item)}
              compact
              active={item.workflow_id === sessionId && statusKind !== "available"}
            />
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
  {/if}
</section>

<style>
  .agent-chat {
    width: 100%;
    height: 100%;
    min-height: 0;
    display: grid;
    gap: 0;
    background: var(--surface-0);
  }

  .agent-chat.full {
    grid-template-columns: minmax(0, 1fr) minmax(280px, 340px);
  }

  .agent-chat.embedded {
    grid-template-columns: minmax(0, 1fr);
  }

  .chat-shell {
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto auto;
    border-right: 1px solid var(--border);
  }

  .agent-chat.embedded .chat-shell {
    border-right: 0;
  }

  .agent-chat.headerless .chat-shell {
    grid-template-rows: minmax(0, 1fr) auto auto;
  }

  .agent-chat-head {
    min-height: 66px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 18px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-1);
  }

  .agent-chat.embedded .agent-chat-head {
    min-height: 58px;
    align-items: flex-start;
    padding: 10px 12px;
  }

  .agent-mark {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
  }

  .agent-mark {
    width: auto;
    height: auto;
  }

  .agent-chat.embedded .agent-mark {
    width: auto;
    height: auto;
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

  .agent-controls {
    min-width: 0;
    margin-left: auto;
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
  }

  .agent-chat.embedded .agent-controls {
    flex: 1 1 100%;
    margin-left: 44px;
    justify-content: flex-start;
  }

  .header-session-add,
  .header-session-drawer {
    --control-accent: var(--accent);
    position: relative;
    min-width: 0;
    height: 28px;
    display: inline-grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 6px;
    padding: 0 8px;
    border: 1px solid color-mix(in srgb, var(--control-accent) 18%, var(--border));
    border-radius: 6px;
    background: var(--control-bg);
    color: var(--text-2);
    cursor: pointer;
    font-size: 11px;
    font-weight: 600;
    box-shadow: inset 0 1px 0 rgb(255 255 255 / 0.04);
    transition:
      border-color 140ms ease,
      background 140ms ease,
      color 140ms ease,
      box-shadow 140ms ease;
  }

  .header-session-drawer {
    --control-accent: var(--reasoning);
  }

  .header-session-add {
    flex: 0 0 auto;
  }

  .header-session-add:hover:not(.disabled),
  .header-session-add:focus-within:not(.disabled),
  .header-session-add.active,
  .header-session-drawer:hover,
  .header-session-drawer:focus-visible,
  .header-session-drawer.active {
    border-color: color-mix(in srgb, var(--control-accent) 46%, var(--border-strong));
    color: var(--text-1);
    background: color-mix(in srgb, var(--control-accent) 10%, var(--control-hover));
    box-shadow:
      inset 0 1px 0 rgb(255 255 255 / 0.06),
      0 0 0 3px color-mix(in srgb, var(--control-accent) 16%, transparent);
    outline: 0;
  }

  .control-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--text-3);
    transition: transform 140ms ease, color 140ms ease;
  }

  .header-session-add.active .control-chevron,
  .header-session-drawer.active .control-chevron {
    color: color-mix(in srgb, var(--control-accent) 78%, white);
    transform: rotate(180deg);
  }

  .header-session-add:hover:not(.disabled) .control-chevron,
  .header-session-add:focus-within:not(.disabled) .control-chevron,
  .header-session-drawer:hover .control-chevron,
  .header-session-drawer:focus-visible .control-chevron {
    color: color-mix(in srgb, var(--control-accent) 78%, white);
  }

  .header-session-add span,
  .header-session-drawer span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .header-session-add.disabled {
    cursor: default;
    opacity: 0.52;
  }

  .new-session-control {
    position: relative;
    display: inline-flex;
  }

  .agent-command-menu {
    position: absolute;
    top: calc(100% + 10px);
    z-index: 30;
    width: min(360px, calc(100vw - 32px));
    display: grid;
    gap: 8px;
    padding: 10px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--surface-1);
    box-shadow: var(--shadow-popover);
  }

  .agent-command-menu.header-menu {
    left: 0;
  }

  .agent-command-menu.panel-menu {
    right: 0;
  }

  .agent-command-row {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 9px;
    align-items: center;
    padding: 9px;
    border: 1px solid color-mix(in srgb, var(--accent) 12%, var(--border));
    border-radius: 7px;
    background: color-mix(in srgb, var(--surface-2) 44%, var(--surface-1));
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .agent-command-row:hover,
  .agent-command-row:focus-visible {
    border-color: color-mix(in srgb, var(--accent) 42%, var(--border-strong));
    background: color-mix(in srgb, var(--accent) 7%, var(--surface-2));
    outline: 0;
  }

  .agent-command-copy {
    min-width: 0;
    display: grid;
    gap: 3px;
  }

  .agent-command-copy strong,
  .agent-command-copy small {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-command-copy strong {
    color: var(--text-1);
    font-size: 12px;
    font-weight: 700;
  }

  .agent-command-copy small {
    color: var(--text-3);
    font-size: 11px;
  }

  .session-drawer {
    min-height: 0;
    overflow: hidden;
    display: grid;
    grid-template-rows: auto auto auto minmax(0, 1fr);
    gap: 10px;
    padding: 14px 12px;
    background: var(--surface-0);
    border-bottom: 1px solid var(--border);
  }

  .session-drawer-head {
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .session-drawer-title {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--text-1);
    font-size: 13px;
    font-weight: 700;
  }

  .session-drawer-close {
    width: 28px;
    height: 28px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--control-bg);
    color: var(--text-3);
    cursor: pointer;
  }

  .session-drawer-close:hover,
  .session-drawer-close:focus-visible {
    color: var(--text-1);
    border-color: var(--border-strong);
    outline: 0;
  }

  .session-drawer-search {
    min-width: 0;
    height: 34px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 8px;
    align-items: center;
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--control-bg);
    color: var(--text-3);
  }

  .session-drawer-search:focus-within {
    border-color: color-mix(in srgb, var(--accent) 48%, var(--border-strong));
    color: var(--text-2);
    box-shadow: 0 0 0 3px var(--focus-ring);
  }

  .session-drawer-search input {
    min-width: 0;
    border: 0;
    outline: 0;
    background: transparent;
    color: var(--text-1);
    font: inherit;
    font-size: 12px;
  }

  .session-drawer-search input::placeholder {
    color: var(--text-3);
  }

  .session-drawer-list {
    min-height: 0;
    overflow-y: auto;
    display: grid;
    align-content: start;
    gap: 8px;
  }

  .drawer-session-row {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 9px;
    align-items: center;
    padding: 10px;
    border: 1px solid color-mix(in srgb, var(--reasoning) 10%, var(--border));
    border-radius: 7px;
    background: color-mix(in srgb, var(--surface-2) 42%, var(--surface-1));
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
    transition:
      border-color 140ms ease,
      background 140ms ease,
      transform 140ms ease;
  }

  .drawer-session-row:hover,
  .drawer-session-row:focus-visible {
    border-color: color-mix(in srgb, var(--reasoning) 38%, var(--border-strong));
    background: color-mix(in srgb, var(--reasoning) 5%, var(--surface-2));
    transform: translateY(-1px);
    outline: 0;
  }

  .drawer-session-row.active {
    border-color: color-mix(in srgb, var(--accent) 54%, var(--border));
    background: color-mix(in srgb, var(--accent) 10%, var(--surface-1));
    box-shadow: inset 3px 0 0 var(--accent);
  }

  .message-list {
    min-height: 0;
    overflow-y: auto;
    overflow-anchor: none;
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 22px clamp(18px, 5vw, 72px);
  }

  .agent-chat.embedded .message-list {
    gap: 12px;
    padding: 14px 12px;
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
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin-top: 2px;
    flex: 0 0 auto;
    border: 1px solid color-mix(in srgb, var(--accent) 22%, transparent);
    border-radius: 8px;
    background: color-mix(in srgb, var(--accent) 16%, var(--surface-2));
    color: var(--accent);
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

  .message.assistant .bubble {
    --markdown-font-family: "SF Pro Text", -apple-system, BlinkMacSystemFont, "Segoe UI",
      Roboto, "Helvetica Neue", Arial, sans-serif;
    --markdown-body-size: 14px;
    --markdown-body-line-height: 1.6;
    --markdown-heading-size: 14.5px;
    --markdown-heading-line-height: 1.42;
    --markdown-block-gap: 11px;
    --markdown-list-gap: 7px;
    --markdown-strong-weight: 680;
  }

  .agent-chat.embedded .bubble {
    max-width: min(100%, 680px);
  }

  .agent-chat.embedded .message.assistant .bubble {
    width: min(calc(100% - 40px), 640px);
  }

  .message.user .bubble {
    max-width: min(620px, 72%);
    border-color: color-mix(in srgb, var(--accent) 32%, transparent);
    background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
  }

  .agent-chat.embedded .message.user .bubble {
    max-width: min(100%, 560px);
  }

  .activity-feed {
    position: relative;
    width: min(720px, 82%);
    display: grid;
    align-self: flex-start;
    margin-left: 40px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: color-mix(in srgb, var(--surface-1) 78%, transparent);
    transition: border-color 160ms ease, background 160ms ease;
  }

  .agent-chat.embedded .activity-feed {
    width: min(100%, 680px);
    margin-left: 0;
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
    padding-top: 2px;
    border-top: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
  }

  .activity-row {
    min-width: 0;
    display: grid;
    gap: 6px;
  }

  .activity-summary,
  .activity-row-button {
    width: 100%;
    padding: 0;
    border: 0;
    border-radius: 6px;
    background: transparent;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .activity-line {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto auto auto;
    gap: 8px;
    align-items: center;
    color: var(--text-3);
    font-size: 12px;
  }

  .activity-line.highlight-error,
  .activity-line.highlight-success {
    margin: -4px -6px;
    padding: 4px 6px;
    border: 1px solid transparent;
    border-radius: 7px;
    color: var(--text-2);
  }

  .activity-line.highlight-error {
    border-color: color-mix(in srgb, var(--error) 30%, transparent);
    background: color-mix(in srgb, var(--error) 15%, transparent);
  }

  .activity-line.highlight-success {
    border-color: color-mix(in srgb, var(--success) 28%, transparent);
    background: color-mix(in srgb, var(--success) 14%, transparent);
  }

  .activity-summary:hover,
  .activity-row-button:hover {
    color: var(--text-2);
  }

  .activity-line.highlight-error:hover {
    border-color: color-mix(in srgb, var(--error) 42%, transparent);
    background: color-mix(in srgb, var(--error) 19%, transparent);
  }

  .activity-line.highlight-success:hover {
    border-color: color-mix(in srgb, var(--success) 38%, transparent);
    background: color-mix(in srgb, var(--success) 18%, transparent);
  }

  .activity-summary:focus-visible,
  .activity-row-button:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
    outline-offset: 2px;
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
  .activity-line.turn-summary .activity-icon { color: var(--accent); }

  .activity-copy {
    min-width: 0;
    overflow: hidden;
  }

  .activity-summary .activity-copy {
    display: grid;
    gap: 3px;
  }

  .activity-row-button .activity-copy {
    display: inline-flex;
    gap: 7px;
    align-items: baseline;
    white-space: nowrap;
  }

  .activity-heading {
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

  .activity-heading span,
  .activity-row-button .activity-copy > span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .activity-message {
    min-width: 0;
    overflow: hidden;
    color: var(--text-2);
    font-size: 12px;
    font-weight: 600;
    line-height: 1.35;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .activity-duration {
    min-width: 38px;
    color: color-mix(in srgb, var(--text-3) 78%, transparent);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    text-align: right;
    white-space: nowrap;
  }

  .activity-line time {
    color: var(--text-3);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .activity-chevron,
  .activity-row-chevron {
    color: var(--text-3);
    transition: transform 160ms ease, color 160ms ease;
  }

  .activity-summary:hover .activity-chevron,
  .activity-row-button:hover .activity-row-chevron {
    color: var(--text-2);
  }

  .activity-summary.expanded .activity-chevron,
  .activity-row.expanded .activity-row-chevron {
    transform: rotate(180deg);
  }

  .activity-detail {
    min-width: 0;
    max-height: 320px;
    overflow: auto;
    margin: 0 0 0 30px;
    padding: 8px 10px;
    border: 1px solid color-mix(in srgb, var(--border) 78%, transparent);
    border-radius: 7px;
    background: var(--surface-0);
    color: var(--text-2);
    font: 11px/1.45 ui-monospace, SFMono-Regular, SFMono, Menlo, Consolas, "Liberation Mono", monospace;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  .activity-script-detail {
    position: relative;
    min-width: 0;
    max-height: 320px;
    overflow: auto;
    margin: 0 0 0 30px;
    padding: 34px 12px 12px;
    border: 1px solid var(--code-block-border);
    border-radius: 8px;
    background: var(--code-block-bg);
    color: var(--code-block-text);
    box-shadow: var(--code-block-shadow);
    font-family:
      SFMono-Regular,
      Consolas,
      "Liberation Mono",
      monospace;
    font-size: 12px;
    line-height: 1.55;
    tab-size: 2;
    white-space: pre;
  }

  .activity-script-detail::before {
    content: attr(data-language);
    position: absolute;
    top: 9px;
    right: 10px;
    padding: 2px 7px;
    border: 1px solid var(--code-label-border);
    border-radius: 999px;
    background: var(--code-label-bg);
    color: var(--code-label-text);
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    font-size: 10px;
    font-weight: 750;
    line-height: 1.2;
    letter-spacing: 0;
  }

  .activity-script-detail code {
    font: inherit;
  }

  .approval-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    margin-left: 30px;
    padding-top: 2px;
  }

  .approval-actions.compact {
    margin-left: 0;
    padding-top: 0;
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

  .approval-actions .approval-remember {
    color: var(--queue);
    border-color: color-mix(in srgb, var(--queue) 40%, var(--border));
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

  .pending-approvals {
    display: grid;
    gap: 8px;
    margin: 0 clamp(18px, 5vw, 72px) 10px;
    padding: 10px;
    border: 1px solid color-mix(in srgb, var(--queue) 42%, var(--border));
    border-radius: 8px;
    background: color-mix(in srgb, var(--queue) 9%, var(--surface-1));
  }

  .agent-chat.embedded .pending-approvals {
    margin: 0 12px 10px;
  }

  .pending-approvals-head {
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .pending-approval-list {
    display: grid;
    gap: 7px;
  }

  .pending-approval-card {
    min-width: 0;
    display: grid;
    gap: 8px;
    padding: 8px;
    border: 1px solid color-mix(in srgb, var(--queue) 26%, var(--border));
    border-radius: 7px;
    background: var(--surface-0);
  }

  .pending-approval-copy {
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: baseline;
  }

  .pending-approval-copy strong {
    color: var(--text-1);
    font-size: 12px;
  }

  .pending-approval-copy span {
    color: var(--text-3);
    font-size: 11px;
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

  .error-banner {
    margin: 0 clamp(18px, 5vw, 72px) 10px;
    padding: 8px 10px;
    border: 1px solid color-mix(in srgb, var(--error) 35%, var(--border));
    border-radius: 7px;
    color: var(--error);
    background: color-mix(in srgb, var(--error) 9%, var(--surface-1));
    font-size: 12px;
  }

  .agent-chat.embedded .error-banner {
    margin: 0 12px 10px;
  }

  .composer-wrap {
    position: relative;
    margin: 0 clamp(18px, 5vw, 72px) 18px;
  }

  .agent-chat.embedded .composer-wrap {
    margin: 0 12px 12px;
  }

  .slash-menu {
    position: absolute;
    right: 0;
    bottom: calc(100% + 8px);
    left: 0;
    z-index: 12;
    display: grid;
    gap: 6px;
    padding: 8px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: color-mix(in srgb, var(--surface-1) 96%, black);
    box-shadow: 0 12px 30px rgb(0 0 0 / 0.3);
  }

  .slash-models {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px;
  }

  .slash-row {
    min-width: 0;
    min-height: 42px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 9px;
    align-items: center;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
    color: var(--text-1);
    text-align: left;
    cursor: pointer;
    font: inherit;
  }

  .slash-row:hover:not(.slash-command-summary),
  .slash-row:focus-visible,
  .slash-row.active {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
    background: color-mix(in srgb, var(--accent) 9%, var(--surface-2));
    outline: 0;
  }

  .slash-row :global(svg) {
    color: var(--accent);
  }

  .slash-command-summary {
    cursor: default;
  }

  .slash-row span {
    min-width: 0;
    display: grid;
    gap: 1px;
  }

  .slash-row strong,
  .slash-row small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .slash-row strong {
    font-size: 12px;
    font-weight: 700;
  }

  .slash-row small {
    color: var(--text-3);
    font-size: 11px;
  }

  .composer {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
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
    --control-accent: var(--accent);
    position: relative;
    height: 30px;
    display: inline-grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 6px;
    padding: 0 8px;
    border: 1px solid color-mix(in srgb, var(--control-accent) 18%, var(--border));
    border-radius: 6px;
    background: var(--control-bg);
    color: var(--text-2);
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
    box-shadow: inset 0 1px 0 rgb(255 255 255 / 0.04);
    transition:
      border-color 140ms ease,
      background 140ms ease,
      color 140ms ease,
      box-shadow 140ms ease;
  }

  .session-add-label {
    min-width: 58px;
    max-width: 128px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .session-add-select:hover:not(.disabled),
  .session-add-select:focus-within:not(.disabled),
  .session-add-select.active {
    border-color: color-mix(in srgb, var(--control-accent) 46%, var(--border-strong));
    color: var(--text-1);
    background: color-mix(in srgb, var(--control-accent) 10%, var(--control-hover));
    box-shadow:
      inset 0 1px 0 rgb(255 255 255 / 0.06),
      0 0 0 3px color-mix(in srgb, var(--control-accent) 16%, transparent);
  }

  .session-add-select.disabled {
    opacity: 0.52;
    cursor: default;
  }

  .session-add-select.active .control-chevron,
  .session-add-select:hover:not(.disabled) .control-chevron,
  .session-add-select:focus-within:not(.disabled) .control-chevron {
    color: color-mix(in srgb, var(--control-accent) 78%, white);
  }

  .session-add-select.active .control-chevron {
    transform: rotate(180deg);
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
    grid-template-columns: minmax(0, 1fr) auto auto;
    gap: 6px;
    align-items: center;
    padding: 6px;
    border: 1px solid color-mix(in srgb, var(--reasoning) 10%, var(--border));
    border-radius: 7px;
    color: inherit;
    background: color-mix(in srgb, var(--surface-2) 72%, var(--surface-1));
    transition:
      border-color 140ms ease,
      background 140ms ease,
      transform 140ms ease;
  }

  .session-card.active {
    border-color: color-mix(in srgb, var(--accent) 54%, var(--border));
    background: color-mix(in srgb, var(--accent) 10%, var(--surface-2));
    box-shadow: inset 3px 0 0 var(--accent);
    cursor: default;
  }

  .session-card:hover,
  .session-card:focus-within {
    border-color: color-mix(in srgb, var(--reasoning) 38%, var(--border-strong));
    background: color-mix(in srgb, var(--reasoning) 5%, var(--surface-2));
    transform: translateY(-1px);
  }

  .session-select {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 9px;
    align-items: center;
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

  .drawer-session-row :global(.status-chip),
  .session-card :global(.status-chip) {
    justify-self: end;
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
    .agent-chat-head {
      flex-wrap: wrap;
    }

    .agent-controls {
      width: 100%;
      margin-left: 48px;
      flex-wrap: wrap;
      justify-content: flex-start;
    }

    .agent-chat.embedded .agent-controls {
      margin-left: 42px;
    }

    .slash-models {
      grid-template-columns: minmax(0, 1fr);
    }

  }

  @media (max-width: 980px) {
    .agent-chat.full {
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

    .composer {
      margin-inline: 16px;
      padding-inline: 0;
    }

    .composer {
      padding: 8px 8px 8px 12px;
    }
  }
</style>
