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
    RotateCw,
    ShieldCheck,
    Sparkles,
    XCircle,
    Wrench
  } from "@lucide/svelte";
  import { tick } from "svelte";
  import type {
    AgentInboundMessage,
    AgentInterfaceFunction,
    FileCitationAnnotation,
    MidTurn,
    Session
  } from "$lib/api/types";
  import { approvalAlreadyResolved } from "$lib/api/httpClient";
  import { formatTokens } from "$lib/cost/pricing";
  import Chip, { type ChipTone } from "$lib/components/primitives/Chip.svelte";
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import StatusChip from "$lib/components/primitives/StatusChip.svelte";
  import { codeModeHostsByRow } from "$lib/state/codeModeNesting";
  import { formatLogValue } from "$lib/state/logValue";
  import { NO_THOUGHT_SUMMARY, foldTurnThought, type TurnThought } from "$lib/state/thoughtSummary";
  import {
    formatElapsedDuration,
    formatTimestamp,
    rowIdentifiers,
    type ReplayLogRow
  } from "$lib/state/replayLog";
  import type { TranscriptItem } from "$lib/state/transcript";
  import type { InterfaceStatus } from "$lib/state/agentRun.svelte";
  import MarkdownMessage from "$lib/components/chat/MarkdownMessage.svelte";
  import SchemaForm from "$lib/components/chat/SchemaForm.svelte";
  import CallInput from "./CallInput.svelte";
  import PendingCallbackCard, { type CallbackOutcome } from "./PendingCallbackCard.svelte";
  import {
    buildPayload,
    describeSchema,
    emptyValues,
    problemSummary,
    singleStringField,
    validate
  } from "$lib/components/chat/schemaForm";

  type MessageTargetRole = "parent" | "subagent";
  /**
   * One agent a message can be addressed to, with its OWN discovered handler surface.
   * A subagent generally accepts different messages than its parent.
   */
  interface MessageTargetOption {
    workflowId: string;
    role: MessageTargetRole;
    label: string;
    agentInterface: AgentInterfaceFunction[];
    /** Absent means loaded: the caller handed the surface over directly. */
    interfaceStatus?: InterfaceStatus;
    closed?: boolean;
  }
  /** A row in the `/` picker: either choose the target agent, or choose one of its handlers. */
  type PickerItem =
    | { kind: "target"; id: string; target: MessageTargetOption }
    | { kind: "handler"; id: string; handler: AgentInterfaceFunction };

  interface Props {
    /** The conversation as of the replay cursor. */
    items: TranscriptItem[];
    logs?: ReplayLogRow[];
    /**
     * The whole run, for what is live state rather than history: pending approvals, the
     * composer's recall, and whether the session has anything in it at all. Default to the
     * view, which is what they are at the live head.
     */
    liveItems?: TranscriptItem[];
    liveLogs?: ReplayLogRow[];
    /** The cursor is at the live head. */
    live?: boolean;
    /** The replay cursor and the run's length, as the transport counts them. */
    viewIndex?: number;
    total?: number;
    onJumpToLive?: () => void;
    sessions?: Session[];
    agentLabel: string;
    sessionId: string;
    agentInterface?: AgentInterfaceFunction[];
    messageTargetLabel?: string;
    messageTargetRole?: MessageTargetRole;
    /** Every agent a message can be sent to — the parent plus any live subagents. */
    messageTargets?: MessageTargetOption[];
    currentAgentWorkflowType?: string | null;
    connecting?: boolean;
    sending?: boolean;
    creatingSession?: boolean;
    closed?: boolean;
    error?: string | null;
    /** Send a message, optionally to a subagent rather than the session's parent. */
    onSend?: (
      message: AgentInboundMessage,
      workflowId?: string | null
    ) => void | Promise<void>;
    /** Stop an agent via the harness close signal — a control action, not a message. */
    onStopAgent?: (workflowId?: string | null) => void | Promise<void>;
    /** Ask an agent for its accepted messages again, after the lookup failed. */
    onRetryInterface?: (workflowId: string) => void | Promise<void>;
    onApproveTool?: (
      workflowId: string,
      toolId: string,
      approved: boolean,
      remember?: boolean
    ) => void | Promise<void>;
    /** Fulfill a pending callback tool call (e.g. answer an `ask_user` question). */
    onCallbackResult?: (
      workflowId: string,
      toolId: string,
      outcome: CallbackOutcome
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
    liveItems,
    liveLogs,
    live = true,
    viewIndex,
    total,
    onJumpToLive,
    sessions = [],
    agentLabel,
    sessionId,
    agentInterface = [],
    messageTargetLabel = "",
    messageTargetRole = "parent",
    messageTargets = [],
    currentAgentWorkflowType = null,
    connecting = false,
    sending = false,
    creatingSession = false,
    closed = false,
    error = null,
    onSend,
    onStopAgent,
    onRetryInterface,
    onApproveTool,
    onCallbackResult
  }: Props = $props();
  let draft = $state("");
  let composerInput = $state<HTMLInputElement | null>(null);
  let historyIndex = $state(-1);
  let historyStash = $state("");
  let localMessages = $state<ChatMessage[]>([]);
  let observedSessionId = $state<string | null>(null);
  let expandedActivityTurns = $state<number[]>([]);
  let expandedLogRows = $state<string[]>([]);
  /* By turn id rather than on the element: the list is unkeyed and re-renders as rows
     arrive or the cursor moves, and a reader's open thought has to outlive both. */
  let openThoughts = $state<string[]>([]);
  let resolvingApprovalIds = $state<string[]>([]);
  let approvalErrors = $state<Record<string, string>>({});
  let decidedApprovalIds = $state<string[]>([]);
  let messageListElement = $state<HTMLDivElement | null>(null);
  let pickerSelectionIndex = $state(0);
  let pickerSignature = $state("");
  // Which agent the composer is addressing (null = the session's parent).
  let messageTargetWorkflowId = $state<string | null>(null);
  // Which handler the composer is addressing. Null until resolved from the target's surface.
  let selectedHandlerName = $state<string | null>(null);
  // Form values for a handler that is not single-string-shaped, keyed by field name.
  let handlerFormValues = $state<Record<string, unknown>>({});
  let handlerFormError = $state<string | null>(null);
  let handlerFormAttempted = $state(false);

  const transcriptMessages = $derived(seedMessages(items));
  const messages = $derived([...transcriptMessages, ...localMessages]);
  const liveMessages = $derived(
    liveItems ? [...seedMessages(liveItems), ...localMessages] : messages
  );
  const runLogs = $derived(liveLogs ?? logs);
  const viewRowIds = $derived(new Set(logs.map((row) => row.id)));
  const sentUserMessages = $derived(
    liveMessages
      .filter((message) => message.role === "user")
      .map((message) => message.text)
  );
  const logsByTurn = $derived(groupLogsByTurn(logs));
  const codeModeHosts = $derived(codeModeHostsByRow(logs));
  const resolvedApprovalKeys = $derived(resolvedApprovalIds(runLogs));
  const pendingApprovalRows = $derived(runLogs.filter((row) => isApprovalPending(row)));
  /* Cleared by `callback_resolved` whoever fulfilled it — this pane, another tab, or a CLI. */
  const resolvedCallbackKeys = $derived(
    new Set(
      runLogs
        .filter((row) => row.event === "callback_resolved")
        .map((row) => approvalKey(row))
    )
  );
  const pendingCallbackRows = $derived(
    runLogs.filter(
      (row) =>
        row.event === "callback_requested" &&
        approvalKey(row) != null &&
        !resolvedCallbackKeys.has(approvalKey(row))
    )
  );
  const sources = $derived(uniqueCitations(messages.flatMap((message) => message.citations)));
  const activeSession = $derived(
    sessions.find((item) => item.workflow_id === sessionId) ?? null
  );
  const targets = $derived.by<MessageTargetOption[]>(() => {
    if (messageTargets.length > 0) return messageTargets;
    return [
      {
        workflowId: sessionId,
        role: messageTargetRole,
        label: messageTargetLabel || agentLabel,
        agentInterface,
        closed
      }
    ];
  });
  const activeTarget = $derived.by(() => {
    const chosen = targets.find(
      (target) => target.workflowId === messageTargetWorkflowId && !target.closed
    );
    if (chosen) return chosen;
    const parent = targets.find((target) => target.role === "parent");
    return parent && !parent.closed ? parent : (targets.find((t) => !t.closed) ?? null);
  });
  const targetsSubagent = $derived(activeTarget?.role === "subagent");
  const showTargetPicker = $derived(targets.filter((t) => !t.closed).length > 1);
  /** Every handler of the selected target — the composer's entire source of truth. */
  const handlers = $derived(activeTarget?.agentInterface ?? []);
  /**
   * The handler the composer will send to. Falls back to the first single-string-shaped one
   * (so a chat-shaped agent opens as a chat box) and otherwise to the first declared handler.
   * Deliberately name-agnostic: no handler is privileged by name.
   */
  const selectedHandler = $derived.by(() => {
    if (handlers.length === 0) return null;
    const chosen = handlers.find((fn) => fn.name === selectedHandlerName);
    if (chosen) return chosen;
    return (
      handlers.find((fn) => singleStringField(fn.parameters) != null) ?? handlers[0]
    );
  });
  /** Non-null when the selected handler renders as a plain text box; the field's name. */
  const textFieldName = $derived(
    selectedHandler ? singleStringField(selectedHandler.parameters) : null
  );
  const handlerFields = $derived(
    selectedHandler && textFieldName == null
      ? describeSchema(selectedHandler.parameters)
      : []
  );
  const canSendToTarget = $derived(
    !closed && activeTarget != null && !activeTarget.closed && handlers.length > 0
  );
  const composerPlaceholder = $derived.by(() => {
    if (closed) return `${agentLabel} is closed`;
    if (activeTarget?.closed) return `${activeTarget.label} is closed`;
    if (handlers.length === 0) {
      const label = activeTarget?.label ?? agentLabel;
      const status = activeTarget?.interfaceStatus ?? "loaded";
      switch (status) {
        case "loading":
          return `Loading the messages ${label} accepts…`;
        case "failed":
          return `Couldn't load the messages ${label} accepts`;
        case "loaded":
          return "This agent declares no messages";
        default: {
          const unhandled: never = status;
          return unhandled;
        }
      }
    }
    if (!selectedHandler) return `Message ${agentLabel}`;
    // The field's own title is the best hint we have, and it comes from the schema — so the
    // prompt reads naturally for `text`, `script`, `prompt`, or anything else.
    const field = textFieldName
      ? describeSchema(selectedHandler.parameters).find((f) => f.name === textFieldName)
      : null;
    const what = field?.title ?? selectedHandler.name;
    return `${what} \u2192 ${activeTarget?.label ?? agentLabel}`;
  });
  /**
   * Whether sending is possible while a turn is already running — the selected handler's own
   * declared mid-turn behavior, not an agent-level setting. `enqueue` queues behind the turn
   * and `accept` joins it, so both stay available; `reject` would fail the update, so the
   * composer disables instead of letting the user discover that the hard way.
   */
  const midTurn = $derived(selectedHandler?.mid_turn ?? "reject");
  const sendingBlocksInput = $derived(sending && midTurn === "reject");
  const connectingBlocksInput = $derived(connecting && activeSession == null);
  const composerDisabled = $derived(closed || connectingBlocksInput || creatingSession);
  const pickerDraft = $derived(parsePickerDraft(draft));
  /**
   * Typing `/` as the first character opens the handler picker. This is purely a client-side
   * convention for a familiar affordance — the harness has no notion of a slash, and the rows
   * are just whatever `agent_interface` returned.
   */
  const pickerOpen = $derived(
    canSendToTarget && draft.trimStart().startsWith("/") && !composerDisabled
  );
  const pickerItems = $derived(
    buildPickerItems(pickerOpen, targets, handlers, pickerDraft, showTargetPicker)
  );
  const composerBusy = $derived(
    sendingBlocksInput || connectingBlocksInput || creatingSession || closed
  );
  const canSendDraft = $derived(
    Boolean(draft.trim()) &&
      canSendToTarget &&
      !composerBusy &&
      textFieldName != null &&
      !draft.trimStart().startsWith("/")
  );
  const canSubmitForm = $derived(
    canSendToTarget && !composerBusy && textFieldName == null && handlerFields.length > 0
  );
  const handlerProblems = $derived(
    handlerFormAttempted ? validate(handlerFields, handlerFormValues) : {}
  );
  const handlerFormAlert = $derived(problemSummary(handlerProblems) ?? handlerFormError);
  const latestMessage = $derived(messages[messages.length - 1] ?? null);
  const latestLog = $derived(logs[logs.length - 1] ?? null);
  const chatScrollSignature = $derived(
    [
      sessionId,
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
      pendingCallbackRows.length,
      Object.keys(approvalErrors).length,
      decidedApprovalIds.length
    ].join("|")
  );
  $effect(() => {
    if (observedSessionId === null) {
      observedSessionId = sessionId;
      return;
    }

    if (observedSessionId !== sessionId) {
      observedSessionId = sessionId;
      draft = "";
      historyIndex = -1;
      historyStash = "";
      localMessages = [];
      expandedActivityTurns = [];
      expandedLogRows = [];
    }
  });

  $effect(() => {
    chatScrollSignature;

    void tick().then(() => {
      scrollMessagesToBottom();
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(scrollMessagesToBottom);
      }
    });
  });

  // Drop a target that went away or was stopped, so the composer falls back to the parent.
  $effect(() => {
    if (
      messageTargetWorkflowId &&
      !targets.some(
        (target) => target.workflowId === messageTargetWorkflowId && !target.closed
      )
    ) {
      messageTargetWorkflowId = null;
    }
  });

  // Keep the selection valid as the target (and therefore its handler surface) changes, and
  // reset the form to the newly selected handler's own fields.
  $effect(() => {
    const resolved = selectedHandler?.name ?? null;
    if (resolved !== selectedHandlerName) {
      selectedHandlerName = resolved;
      handlerFormError = null;
      handlerFormAttempted = false;
      handlerFormValues = resolved && textFieldName == null
        ? emptyValues(describeSchema(selectedHandler!.parameters))
        : {};
    }
  });

  $effect(() => {
    const signature = pickerItems.map((item) => item.id).join("|");
    if (signature !== pickerSignature) {
      pickerSignature = signature;
      pickerSelectionIndex = defaultPickerSelectionIndex(pickerItems);
      return;
    }

    if (pickerItems.length === 0) {
      pickerSelectionIndex = 0;
    } else if (pickerSelectionIndex >= pickerItems.length) {
      pickerSelectionIndex = pickerItems.length - 1;
    }
  });

  function seedMessages(transcriptItems: TranscriptItem[]): ChatMessage[] {
    const messages: ChatMessage[] = [];
    const emittedUsers = new Set<number>();

    for (const item of transcriptItems) {
      if (item.kind === "user") {
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
    // Rows the transcript already renders in full, plus the pure lifecycle brackets — the
    // in-app log is the "what else happened" column, not a second copy of the conversation.
    return ![
      "message_accepted",
      "message_handler_start",
      "message_handler_end",
      "turn_started",
      "turn_end",
      "reply_delta",
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
      const key = approvalKey(row);
      if (row.event === "tool_approval_resolved" && key) result.add(key);
    }
    return result;
  }

  function approvalWorkflowId(row: ReplayLogRow): string {
    return row.workflowId ?? sessionId;
  }

  function approvalKey(row: ReplayLogRow): string | null {
    return row.toolId ? `${approvalWorkflowId(row)}:${row.toolId}` : null;
  }

  function isApprovalPending(row: ReplayLogRow): boolean {
    const key = approvalKey(row);
    return (
      row.event === "tool_approval_requested" &&
      key != null &&
      !resolvedApprovalKeys.has(key)
    );
  }

  function isApprovalResolving(row: ReplayLogRow): boolean {
    const key = approvalKey(row);
    return key != null && resolvingApprovalIds.includes(key);
  }

  function approvalError(row: ReplayLogRow): string | null {
    const key = approvalKey(row);
    return key ? approvalErrors[key] ?? null : null;
  }

  function approvalDecidedElsewhere(row: ReplayLogRow): boolean {
    const key = approvalKey(row);
    return key != null && decidedApprovalIds.includes(key);
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
      detail: `total ${formatTokens(turnTokens(rows))} tok`,
      duration: durationMs > 0 ? formatElapsedDuration(durationMs) : null,
      endedAt: Number.isFinite(endedAt) ? endedAt : visibleRows[visibleRows.length - 1]?.timestamp ?? 0
    };
  }

  function turnTokens(rows: ReplayLogRow[]): number {
    return rows.reduce((sum, row) => sum + (row.usage?.total ?? 0), 0);
  }

  function scrollMessagesToBottom(): void {
    const element = messageListElement;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
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

  function setThoughtOpen(turnId: string, open: boolean): void {
    if (open === openThoughts.includes(turnId)) return;
    openThoughts = open
      ? [...openThoughts, turnId]
      : openThoughts.filter((item) => item !== turnId);
  }

  function thoughtLabel(thought: TurnThought): string {
    return thought.seconds > 0
      ? `Thought ${formatElapsedDuration(thought.seconds * 1000)}`
      : "Thought";
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

  function logDetail(row: ReplayLogRow): string {
    const value = row.body ?? row.status ?? row.output ?? "";
    return value.split(/\r?\n/)[0]?.trim() ?? "";
  }

  function turnMessagePreview(value: string): string {
    return value.trim().replace(/\s+/g, " ") || "No user message";
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
    const key = approvalKey(row);
    if (!toolId || !key || !onApproveTool || isApprovalResolving(row)) return;

    resolvingApprovalIds = [...resolvingApprovalIds, key];
    approvalErrors = { ...approvalErrors, [key]: "" };
    try {
      await onApproveTool(approvalWorkflowId(row), toolId, approved, remember);
    } catch (error) {
      if (approvalAlreadyResolved(error)) {
        decidedApprovalIds = [...decidedApprovalIds, key];
      } else {
        approvalErrors = {
          ...approvalErrors,
          [key]: error instanceof Error ? error.message : "Approval request failed."
        };
      }
    } finally {
      resolvingApprovalIds = resolvingApprovalIds.filter((item) => item !== key);
    }
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

  /** Split a `/name ...` draft into the typed handler-name prefix and the rest. */
  function parsePickerDraft(value: string): { name: string; rest: string } {
    const trimmed = value.trimStart();
    if (!trimmed.startsWith("/")) return { name: "", rest: "" };
    const [name = "", ...rest] = trimmed.slice(1).split(/\s+/);
    return { name: name.toLowerCase(), rest: rest.join(" ").trim() };
  }

  function handlerMatchesDraft(handler: AgentInterfaceFunction, typed: string): boolean {
    return !typed || handler.name.toLowerCase().startsWith(typed);
  }

  /**
   * Rows for the `/` picker: the target agent first when there is a choice of them, then the
   * selected target's handlers filtered by what has been typed.
   */
  function buildPickerItems(
    open: boolean,
    allTargets: MessageTargetOption[],
    available: AgentInterfaceFunction[],
    draftParts: { name: string; rest: string },
    includeTargets: boolean
  ): PickerItem[] {
    if (!open) return [];
    const items: PickerItem[] = [];
    if (includeTargets) {
      items.push(
        ...allTargets
          .filter((target) => !target.closed)
          .map((target) => ({
            kind: "target" as const,
            id: `target:${target.workflowId}`,
            target
          }))
      );
    }
    items.push(
      ...available
        .filter((handler) => handlerMatchesDraft(handler, draftParts.name))
        .map((handler) => ({
          kind: "handler" as const,
          id: `handler:${handler.name}`,
          handler
        }))
    );
    return items;
  }

  function defaultPickerSelectionIndex(items: PickerItem[]): number {
    const firstHandler = items.findIndex((item) => item.kind === "handler");
    return firstHandler >= 0 ? firstHandler : 0;
  }

  /** How a handler's mid-turn behavior reads in the picker and the composer badge. */
  function midTurnLabel(mode: AgentInterfaceFunction["mid_turn"]): string {
    if (mode === "enqueue") return "queues";
    if (mode === "accept") return "joins";
    return "needs idle";
  }

  function midTurnTone(mode: MidTurn): ChipTone {
    switch (mode) {
      case "enqueue":
        return "neutral";
      case "accept":
        return "success";
      case "reject":
        return "retry";
      default: {
        const unhandled: never = mode;
        return unhandled;
      }
    }
  }

  function midTurnHint(mode: AgentInterfaceFunction["mid_turn"]): string {
    if (mode === "enqueue") return "Sent while busy: waits its turn behind the current work.";
    if (mode === "accept") return "Sent while busy: joins the running turn and applies now.";
    return "Sent while busy: refused — this message needs an idle agent.";
  }

  function selectTarget(target: MessageTargetOption): void {
    messageTargetWorkflowId = target.workflowId;
    selectedHandlerName = null;
    draft = "/";
    composerInput?.focus();
  }

  function selectHandler(handler: AgentInterfaceFunction): void {
    selectedHandlerName = handler.name;
    handlerFormError = null;
    handlerFormAttempted = false;
    const single = singleStringField(handler.parameters);
    handlerFormValues = single ? {} : emptyValues(describeSchema(handler.parameters));
    // Clear the `/` draft: the handler is chosen now, so the box (or the form) takes over.
    draft = "";
    composerInput?.focus();
  }

  /**
   * Send the text box's contents to the selected handler.
   *
   * Only reachable when the handler is single-string-shaped; anything else goes through
   * `submitHandlerForm`. The payload's field name comes from the schema, so this works for a
   * handler whose field is `text`, `script`, `prompt`, or anything else.
   */
  async function sendMessage(text = draft): Promise<void> {
    const question = text.trim();
    const handler = selectedHandler;
    const field = textFieldName;
    if (
      !question ||
      question.startsWith("/") ||
      !handler ||
      !field ||
      !canSendToTarget ||
      composerBusy
    ) {
      return;
    }

    draft = "";
    historyIndex = -1;
    await dispatchMessage(handler, { [field]: question }, question);
  }

  /** Send the schema-driven form's values to the selected handler. */
  async function submitHandlerForm(): Promise<void> {
    const handler = selectedHandler;
    if (!handler || !canSubmitForm) return;
    handlerFormAttempted = true;
    handlerFormError = null;
    if (Object.keys(handlerProblems).length > 0) return;
    let payload;
    try {
      payload = buildPayload(handlerFields, handlerFormValues);
    } catch (error) {
      handlerFormError =
        error instanceof Error ? error.message : "Could not build the payload.";
      return;
    }
    await dispatchMessage(handler, payload, summarizePayload(handler.name, payload));
    handlerFormAttempted = false;
    handlerFormValues = emptyValues(handlerFields);
  }

  function summarizePayload(name: string, payload: Record<string, unknown>): string {
    const entries = Object.entries(payload);
    const strings = entries.filter(([, v]) => typeof v === "string");
    if (entries.length === 1 && strings.length === 1) return strings[0][1] as string;
    const rendered = entries
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join(", ");
    return `${name}(${rendered})`;
  }

  async function dispatchMessage(
    handler: AgentInterfaceFunction,
    payload: Record<string, unknown>,
    displayText: string
  ): Promise<void> {
    const outbound: AgentInboundMessage = { type: handler.name, payload };
    const target = activeTarget;
    if (onSend) {
      await onSend(outbound, target?.workflowId ?? null);
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

  function selectedPickerItem(): PickerItem | null {
    return pickerItems[pickerSelectionIndex] ?? pickerItems[0] ?? null;
  }

  function pickerItemActive(item: PickerItem): boolean {
    return selectedPickerItem()?.id === item.id;
  }

  function acceptPickerSelection(): boolean {
    if (!pickerOpen) return false;
    const selected = selectedPickerItem();
    if (!selected) return false;
    if (selected.kind === "target") {
      selectTarget(selected.target);
      return true;
    }
    selectHandler(selected.handler);
    return true;
  }

  function movePickerSelection(delta: number): boolean {
    if (!pickerOpen || pickerItems.length === 0) return false;
    pickerSelectionIndex =
      (pickerSelectionIndex + delta + pickerItems.length) % pickerItems.length;
    return true;
  }

  function resetHistoryRecall(): void {
    historyIndex = -1;
  }

  // Shell-style recall of the messages already sent this session.
  // step -1 moves toward older messages (ArrowUp), +1 toward newer (ArrowDown).
  function recallHistory(step: number): boolean {
    const history = sentUserMessages;
    if (history.length === 0) return false;

    if (historyIndex === -1) {
      if (step > 0) return false; // ArrowDown with no active recall: leave draft alone
      historyStash = draft;
      historyIndex = history.length - 1;
      draft = history[historyIndex] ?? "";
      return true;
    }

    const next = historyIndex + step;
    if (next < 0) return true; // already at oldest; consume the key but stay put
    if (next >= history.length) {
      // moved past the newest entry: restore the in-progress draft
      historyIndex = -1;
      draft = historyStash;
      return true;
    }
    historyIndex = next;
    draft = history[historyIndex] ?? "";
    return true;
  }

  async function moveCaretToEnd(): Promise<void> {
    await tick();
    const element = composerInput;
    if (!element) return;
    const end = element.value.length;
    element.setSelectionRange(end, end);
  }

  function handleComposerKeydown(event: KeyboardEvent): void {
    if (event.altKey || event.ctrlKey || event.metaKey) return;

    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      if (movePickerSelection(1)) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (event.key === "ArrowDown" && recallHistory(1)) {
        event.preventDefault();
        event.stopPropagation();
        void moveCaretToEnd();
      }
      return;
    }

    if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      if (movePickerSelection(-1)) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (event.key === "ArrowUp" && recallHistory(-1)) {
        event.preventDefault();
        event.stopPropagation();
        void moveCaretToEnd();
      }
      return;
    }

    if ((event.key === "Tab" && !event.shiftKey) || event.key === "Enter") {
      if (!acceptPickerSelection()) return;
      event.preventDefault();
      event.stopPropagation();
      return;
    }

    // Any other key (typing, backspace, escape, …) ends history recall so the
    // next ArrowUp re-stashes the edited draft instead of overwriting it.
    resetHistoryRecall();
  }

  /* Only with more than one handler or target, so a single-handler chat agent still looks
     like a plain chat box. */
  const showTarget = $derived(
    selectedHandler != null && (handlers.length > 1 || targetsSubagent || showTargetPicker)
  );
  const showStop = $derived(onStopAgent != null && activeTarget != null && !activeTarget.closed);
  const showRetry = $derived(
    onRetryInterface != null && activeTarget?.interfaceStatus === "failed" && !activeTarget.closed
  );

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void sendMessage();
  }

</script>

<!-- `embedded headerless` are written out rather than derived: the pane is the
     only place this renders, so both were always what the one call site asked
     for. The rules keyed off them are still the live ones — flattening them into
     the base selectors is a restyle, not a deletion, so the classes stay. -->
<section
  class={`agent-chat embedded headerless ${closed ? "closed" : ""}`}
  aria-label={`${agentLabel} customer chat`}
>
  <div class="chat-shell">
    {#if !live}
      <!-- The Decisions pane's rule: a view parked behind the run says so, and offers the
           way back. Laid over the scroller rather than in the grid, so arriving and leaving
           never resizes the message list under the reader. -->
      <div class="replay-strip">
        <span class="kicker">
          Replay{#if viewIndex != null && total != null}&nbsp;· event {viewIndex} / {total}{/if}
        </span>
        {#if onJumpToLive}
          <Chip
            size="xs"
            fill="quiet"
            aria-label="Jump to latest step"
            title="Jump to latest step"
            onclick={onJumpToLive}
          >
            Latest
          </Chip>
        {/if}
      </div>
    {/if}
    <div class="message-list" bind:this={messageListElement}>
      {#if connecting && liveMessages.length === 0}
        <div class="empty-chat">
          <Sparkles size={18} />
          <span>Connecting to {agentLabel}...</span>
        </div>
      {:else if closed && liveMessages.length === 0}
        <div class="empty-chat closed-empty">
          <CheckCircle2 size={18} />
          <span>{agentLabel} is closed.</span>
        </div>
      {:else if error && liveMessages.length === 0}
        <div class="empty-chat error">
          <Copyable value={error} label="Copy error"><span>{error}</span></Copyable>
        </div>
      {:else if runLogs.length === 0 && liveMessages.length === 0}
        <!-- Attached, served, and carrying nothing: the case the three branches
             above left as a blank pane, which is what an operator returned to a
             session from a probe run sees. Said in the same place a missing
             worker is said, because the reader's question is the same one.

             Gated on the whole run rather than the view: `messages` empties
             again at scrub position zero, and a 1,830-frame session parked at
             its start has plenty to show. -->
        <div class="empty-chat">
          <MessageCircle size={18} />
          <span>No events yet. Ask {agentLabel} something to start this session.</span>
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
            <MarkdownMessage text={message.text} citations={message.citations} />
          </div>
        </article>

        {#if message.role === "user"}
          {@const activityLogs = logsForTurn(message.turnNumber)}
          {@const activeLog = activeLogForTurn(message.turnNumber)}
          {@const expanded = activityExpanded(message.turnNumber)}
          {#if activityLogs.length > 0 && activeLog}
            {@const turnSummary = turnActivitySummary(message.turnNumber, activityLogs)}
            {@const thought = foldTurnThought(activityLogs)}
            <div class={`activity-feed ${expanded ? "expanded" : ""}`}>
              <button
                type="button"
                class={`activity-summary ${expanded ? "expanded" : ""} activity-line turn-summary active`}
                aria-expanded={expanded}
                aria-label={expanded ? "Collapse activity logs" : "Expand activity logs"}
                onclick={() => toggleActivity(message.turnNumber)}
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
                <time>{formatTimestamp(turnSummary.endedAt)}</time>
                <ChevronDown class="activity-chevron" size={14} aria-hidden="true" />
              </button>

              {#if expanded}
                <div class="activity-list">
                  {#each activityLogs as log (log.id)}
                    {#if log.actor === "reasoning" && thought}
                      {#if log.id === thought.firstRowId}
                        <details
                          class="activity-row thought"
                          open={openThoughts.includes(thought.turnId)}
                          ontoggle={(event) => setThoughtOpen(thought.turnId, event.currentTarget.open)}
                        >
                          <summary class="thought-summary">
                            <span class="thought-label">{thoughtLabel(thought)}</span>
                            {#if thought.thinking}
                              <StatusChip label="thinking" kind="reasoning" active compact />
                            {/if}
                          </summary>
                          <p class="thought-text">{thought.text.trim() || NO_THOUGHT_SUMMARY}</p>
                        </details>
                      {/if}
                    {:else}
                      {@const rowExpanded = logExpanded(log)}
                      {@const fullDetail = logFullDetail(log)}
                      {@const scriptDetail = logScript(log)}
                      {@const rowDuration = logElapsedDuration(log, activityLogs)}
                      {@const nested = codeModeHosts.has(log.id)}
                      <div
                        class={`activity-row ${rowExpanded ? "expanded" : ""} ${nested ? "nested" : ""}`}
                      >
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
                            {#if nested}
                              <StatusChip label="host call" kind="tool" compact />
                            {/if}
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
                          <time>{formatTimestamp(log.timestamp)}</time>
                          <ChevronDown class="activity-row-chevron" size={13} aria-hidden="true" />
                        </button>

                        {#if rowExpanded}
                          {@const ids = rowIdentifiers(log)}
                          {#if scriptDetail}
                            <div class="script-detail-wrap"><pre class="activity-script-detail" data-language="python"><code>{scriptDetail}</code></pre></div>
                          {/if}
                          {#if ids.length}
                            <dl class="row-ids">
                              {#each ids as id (id.label)}
                                <div>
                                  <dt>{id.label}</dt>
                                  <dd>
                                    <Copyable value={id.value} label={`Copy ${id.label} ID`}>
                                      <code title={id.value}>{id.value}</code>
                                    </Copyable>
                                  </dd>
                                </div>
                              {/each}
                            </dl>
                          {/if}
                          <section class="activity-full" aria-label="Full details">
                            <Copyable value={fullDetail} label="Copy full details">
                              <span class="activity-full-label">Full details</span>
                            </Copyable>
                            <pre class="activity-detail">{fullDetail}</pre>
                          </section>
                        {/if}
                      </div>
                    {/if}
                  {/each}
                </div>
              {/if}
            </div>
          {/if}
        {/if}
      {/each}

      {#if sending && !closed && live}
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

    {#if !closed && error && liveMessages.length > 0}
      <div class="error-banner">
        <Copyable value={error} label="Copy error"><span>{error}</span></Copyable>
      </div>
    {/if}

    {#if closed}
      <div class="closed-banner">
        <CheckCircle2 size={14} aria-hidden="true" />
        <span>This agent is closed. Start a new session to continue.</span>
      </div>
    {/if}

    {#if pendingApprovalRows.length > 0 || pendingCallbackRows.length > 0}
      <div class="pending-stack">
        {#if pendingApprovalRows.length > 0}
          <!-- Chip, then a card per gated call: identity, the details, the answer.
               That is the thing that scales — a second approval is another card, not
               another kind of layout — and it is what the rest of the harness already
               does with a tool that needs a decision. -->
          <section class="pending-approvals" aria-label="Pending tool approvals">
            <StatusChip
              label={`${pendingApprovalRows.length} approval${
                pendingApprovalRows.length === 1 ? "" : "s"
              } needed`}
              kind="approval"
              active
            />

            {#each pendingApprovalRows as approval (approvalKey(approval) ?? approval.ordinal)}
              <article class="pending-approval-card">
                <header class="pending-approval-head">
                  <strong>{approval.toolName || approval.body || "Tool approval"}</strong>
                  <span>Turn {approval.turnNumber} · {formatTimestamp(approval.timestamp)}</span>
                </header>
                {#if approval.toolId}
                  <dl class="row-ids">
                    <div>
                      <dt>tool call</dt>
                      <dd>
                        <Copyable value={approval.toolId} label="Copy tool call ID">
                          <code title={approval.toolId}>{approval.toolId}</code>
                        </Copyable>
                      </dd>
                    </div>
                  </dl>
                {/if}
                <CallInput input={approval.input} />
                {#if !viewRowIds.has(approval.id)}
                  <!-- Live state, not history: the agent is blocked on this now, so it stays
                       answerable however far back the cursor is. -->
                  <p class="approval-note">Requested ahead of the replay cursor · waiting on you now</p>
                {/if}
                <div class="approval-actions">
                  <Chip
                    fill="quiet"
                    disabled={!onApproveTool || isApprovalResolving(approval)}
                    onclick={(event) => void resolveApproval(event, approval, true)}
                    onkeydown={(event: KeyboardEvent) => event.stopPropagation()}
                  >
                    {#snippet lead()}
                      <CheckCircle2 size={13} />
                    {/snippet}
                    Approve
                  </Chip>
                  <Chip
                    fill="quiet"
                    disabled={!onApproveTool || isApprovalResolving(approval)}
                    onclick={(event) => void resolveApproval(event, approval, true, true)}
                    onkeydown={(event: KeyboardEvent) => event.stopPropagation()}
                  >
                    {#snippet lead()}
                      <ShieldCheck size={13} />
                    {/snippet}
                    Always allow
                  </Chip>
                  <Chip
                    tone="error"
                    fill="quiet"
                    toned
                    disabled={!onApproveTool || isApprovalResolving(approval)}
                    onclick={(event) => void resolveApproval(event, approval, false)}
                    onkeydown={(event: KeyboardEvent) => event.stopPropagation()}
                  >
                    {#snippet lead()}
                      <XCircle size={13} />
                    {/snippet}
                    Reject
                  </Chip>
                </div>
                {#if approvalDecidedElsewhere(approval)}
                  <p class="approval-note">Already decided</p>
                {:else if approvalError(approval)}
                  <p class="approval-error" role="alert">{approvalError(approval)}</p>
                {/if}
              </article>
            {/each}
          </section>
        {/if}

        {#if pendingCallbackRows.length > 0}
          <section class="pending-approvals" aria-label="Pending callback requests">
            <StatusChip
              label={`${pendingCallbackRows.length} response${
                pendingCallbackRows.length === 1 ? "" : "s"
              } needed`}
              kind="approval"
              active
            />
            {#each pendingCallbackRows as callback (approvalKey(callback))}
              <PendingCallbackCard
                row={callback}
                ahead={!viewRowIds.has(callback.id)}
                onSubmit={onCallbackResult && callback.toolId
                  ? (outcome) =>
                      onCallbackResult(approvalWorkflowId(callback), callback.toolId!, outcome)
                  : undefined}
              />
            {/each}
          </section>
        {/if}
      </div>
    {/if}

    <div class="composer-wrap">
      {#if pickerOpen}
        <section class="handler-picker" aria-label="Accepted messages">
          <p class="picker-hint">
            {handlers.length === 0
              ? "This agent declares no accepted messages."
              : `Send to ${activeTarget?.label ?? agentLabel}`}
          </p>
          {#each pickerItems as item (item.id)}
            {#if item.kind === "target"}
              <button
                type="button"
                class="picker-row target"
                class:active={pickerItemActive(item)}
                onclick={() => selectTarget(item.target)}
              >
                <strong>{item.target.label}</strong>
                <small>
                  {item.target.role === "parent" ? "this session" : "subagent"} ·
                  {item.target.agentInterface.length} message{item.target.agentInterface
                    .length === 1
                    ? ""
                    : "s"}
                </small>
              </button>
            {:else}
              <button
                type="button"
                class="picker-row"
                class:active={pickerItemActive(item)}
                title={midTurnHint(item.handler.mid_turn)}
                onclick={() => selectHandler(item.handler)}
              >
                <span class="picker-name">
                  <strong>{item.handler.name}</strong>
                  <span class="mid-turn {item.handler.mid_turn}">
                    {midTurnLabel(item.handler.mid_turn)}
                  </span>
                </span>
                <small>{item.handler.description}</small>
              </button>
            {/if}
          {/each}
        </section>
      {/if}

      <!-- Which handler is being addressed and what sending mid-turn will do, beside the stop
           control. -->
      {#if showTarget || showStop || showRetry}
        <div class="composer-toolbar">
          {#if showTarget && selectedHandler}
            <Chip
              class="target-chip"
              tone="model"
              fill="quiet"
              toned
              disabled={composerDisabled}
              title="Choose which message to send (or type / in the box)"
              onclick={() => {
                draft = "/";
                composerInput?.focus();
              }}
            >
              <span class="target-name">
                {selectedHandler.name}{#if targetsSubagent || showTargetPicker}&nbsp;&rarr;
                  {activeTarget?.label ?? agentLabel}{/if}
              </span>
              <ChevronDown size={12} aria-hidden="true" />
            </Chip>
            <Chip
              tone={midTurnTone(selectedHandler.mid_turn)}
              fill="quiet"
              toned={selectedHandler.mid_turn !== "enqueue"}
              label={midTurnLabel(selectedHandler.mid_turn)}
              title={midTurnHint(selectedHandler.mid_turn)}
            />
          {/if}
          {#if showRetry}
            <Chip
              fill="quiet"
              title="Ask the agent for the messages it accepts again"
              onclick={() => activeTarget && void onRetryInterface?.(activeTarget.workflowId)}
            >
              {#snippet lead()}
                <RotateCw size={13} />
              {/snippet}
              Retry
            </Chip>
          {/if}
          {#if showStop}
            <!-- Stopping is a control-plane action (the harness close signal), not a message —
                 so it works whatever the agent happens to accept. -->
            <Chip
              class="stop-chip"
              tone="error"
              fill="quiet"
              toned
              disabled={creatingSession}
              aria-label={`Stop ${activeTarget?.label ?? agentLabel}`}
              title={`Stop ${activeTarget?.label ?? agentLabel}`}
              onclick={() => void onStopAgent?.(activeTarget?.workflowId ?? null)}
            >
              {#snippet lead()}
                <XCircle size={13} />
              {/snippet}
              Stop
            </Chip>
          {/if}
        </div>
      {/if}

      {#if selectedHandler && textFieldName == null && handlerFields.length > 0}
        <!-- The selected handler takes more than a single string, so it is rendered as a form
             generated from its input JSON Schema. Enum fields become dropdowns. -->
        <form
          class="handler-form"
          onsubmit={(event) => {
            event.preventDefault();
            void submitHandlerForm();
          }}
        >
          <SchemaForm
            fields={handlerFields}
            bind:values={handlerFormValues}
            disabled={composerDisabled}
            idPrefix={`handler-${selectedHandler.name}`}
            errors={handlerProblems}
          />
          {#if handlerFormAlert}
            <p class="form-error" role="alert">{handlerFormAlert}</p>
          {/if}
          <div class="form-actions">
            <button type="submit" class="form-send" disabled={!canSubmitForm}>
              Send {selectedHandler.name}
            </button>
          </div>
        </form>
      {:else}
        <form class="composer" class:closed={closed} onsubmit={handleSubmit}>
          <input
            bind:this={composerInput}
            bind:value={draft}
            placeholder={composerPlaceholder}
            aria-label={`Message ${activeTarget?.label ?? agentLabel}`}
            disabled={composerDisabled || !canSendToTarget}
            onkeydown={handleComposerKeydown}
          />
          <IconButton
            type="submit"
            label="Send message"
            tone="primary"
            disabled={!canSendDraft}
          >
            <ArrowUp size={17} />
          </IconButton>
        </form>
      {/if}

    </div>
  </div>

</section>

<style>
  .agent-chat {
    container: agent-chat / inline-size;
    width: 100%;
    height: 100%;
    min-height: 0;
    display: grid;
    gap: 0;
    background: var(--surface-0);
  }

  .agent-chat.embedded {
    grid-template-columns: minmax(0, 1fr);
  }

  /* Every grid in the chat pins its one column to `minmax(0, 1fr)`. Left implicit it is
     `auto`, which grows to its widest child's min-content — an approval card, a long tool
     input — and carries everything else past the pane's edge with it. */
  .chat-shell {
    position: relative;
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr) auto auto;
    border-right: 1px solid var(--border);
  }

  .agent-chat.embedded .chat-shell {
    border-right: 0;
  }

  /* Named rows, so an absent banner or pending stack never moves the composer into a row
     sized for something else. The pane clips its bottom edge, so when it is short only the
     pending stack gives way, scrolling inside its row while the transcript keeps a floor. */
  .agent-chat.headerless .chat-shell {
    grid-template-rows: minmax(min(96px, 25%), 1fr) auto minmax(0, auto) auto;
    grid-template-areas: "messages" "banner" "pending" "composer";
  }

  .message-list {
    grid-area: messages;
    min-height: 0;
    overflow-y: auto;
    overflow-anchor: none;
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 22px clamp(18px, 5vw, 72px);
    /* Whatever a message stacks — a citation's tooltip — stays under the replay strip. */
    isolation: isolate;
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
    border-radius: var(--radius-md);
    color: var(--text-2);
    background: var(--surface-1);
    font-size: var(--font-lg);
  }

  .empty-chat.error {
    color: var(--error);
    border-color: color-mix(in srgb, var(--error) 35%, var(--border));
  }

  .empty-chat.closed-empty {
    color: var(--success);
    border-color: color-mix(in srgb, var(--success) 32%, var(--border));
    background: color-mix(in srgb, var(--success) 8%, var(--surface-1));
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
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--accent) 16%, var(--surface-2));
    color: var(--accent);
  }

  .bubble {
    max-width: min(720px, 82%);
    min-width: 0;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-1);
    color: var(--text-1);
    line-height: 1.5;
  }

  .message.assistant .bubble {
    --markdown-font-family: var(--font-sans);
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
    grid-template-columns: minmax(0, 1fr);
    align-self: flex-start;
    margin-left: 40px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--surface-1) 78%, transparent);
    transition: border-color var(--duration-fast) var(--ease-ui), background var(--duration-fast) var(--ease-ui);
  }

  .agent-chat.embedded .activity-feed {
    width: min(100%, 680px);
    margin-left: 0;
  }

  .activity-feed:focus-visible {
    border-color: var(--border-strong);
    background: color-mix(in srgb, var(--surface-2) 78%, transparent);
  }

  @media (hover: hover) and (pointer: fine) {
    .activity-feed:hover {
      border-color: var(--border-strong);
      background: color-mix(in srgb, var(--surface-2) 78%, transparent);
    }
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

  .thought-summary {
    display: inline-flex;
    align-items: center;
    gap: var(--gap-sm);
    width: max-content;
    list-style: none;
    cursor: pointer;
  }

  .thought-summary::-webkit-details-marker {
    display: none;
  }

  .thought-summary:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
    outline-offset: 2px;
  }

  .thought-label {
    padding: 2px 6px;
    border: 1px solid color-mix(in srgb, var(--reasoning) 40%, var(--border));
    border-radius: var(--radius-sm);
    background: var(--surface-1);
    color: var(--reasoning);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    letter-spacing: var(--label-tracking);
    text-transform: uppercase;
  }

  .thought-text {
    margin: 0 0 0 30px;
    color: var(--text-3);
    font-size: var(--font-sm);
    line-height: 1.5;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  /* A call a Code Mode script made, hung off the host's icon column. */
  .activity-row.nested {
    margin-left: 10px;
    padding-left: 12px;
    border-left: 1px solid var(--border);
  }

  /* The chip names why the row is indented, so it keeps its words; the tool name after it
     is what gives way. `Chip` allows itself to shrink, which suits a lone chip, not this. */
  .activity-row-button .activity-copy > :global(.chip) {
    flex-shrink: 0;
  }

  /* At the default chat width a label, the chip and a tool name do not fit on one line beside
     the duration and time, so the name wraps below them rather than vanishing. */
  @container agent-chat (max-width: 480px) {
    .activity-row.nested {
      margin-left: 4px;
      padding-left: 6px;
    }

    .activity-row.nested .activity-copy {
      flex-wrap: wrap;
      row-gap: 2px;
    }
  }

  .activity-summary,
  .activity-row-button {
    width: 100%;
    padding: 0;
    border: 0;
    border-radius: var(--radius-sm);
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
    font-size: var(--font-md);
  }

  .activity-line.highlight-error,
  .activity-line.highlight-success {
    margin: -4px -6px;
    padding: 4px 6px;
    border: 1px solid transparent;
    border-radius: var(--radius-md);
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

  @media (hover: hover) and (pointer: fine) {
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
    border-radius: var(--radius-sm);
    background: var(--surface-0);
  }

  .activity-line.active .activity-icon {
    border-color: color-mix(in srgb, currentColor 44%, var(--border));
    box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 12%, transparent);
  }

  .activity-line.model .activity-icon { color: var(--model); }
  .activity-line.reasoning .activity-icon { color: var(--reasoning); }
  .activity-line.tool .activity-icon { color: var(--tool); }
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
    font-size: var(--font-md);
    font-weight: 600;
    line-height: 1.35;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .activity-duration {
    min-width: 38px;
    color: color-mix(in srgb, var(--text-3) 78%, transparent);
    font-size: var(--font-sm);
    font-variant-numeric: tabular-nums;
    text-align: right;
    white-space: nowrap;
  }

  .activity-line time {
    color: var(--text-3);
    font-size: var(--font-sm);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .activity-chevron,
  .activity-row-chevron {
    color: var(--text-3);
    transition: transform var(--duration-fast) var(--ease-ui), color var(--duration-fast) var(--ease-ui);
  }

  @media (hover: hover) and (pointer: fine) {
    .activity-summary:hover .activity-chevron,
    .activity-row-button:hover .activity-row-chevron {
      color: var(--text-2);
    }
  }

  .activity-summary.expanded .activity-chevron,
  .activity-row.expanded .activity-row-chevron {
    transform: rotate(180deg);
  }

  .activity-full {
    min-width: 0;
    display: grid;
    gap: 4px;
    margin-left: 30px;
  }

  .activity-full-label {
    color: var(--text-3);
    font-size: var(--font-sm);
    font-weight: 650;
  }

  /* Same look as the Logs pane's ID list: one label column as wide as its widest
     label, so the values line up whichever IDs a row has. */
  .row-ids {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 2px 6px;
    min-width: 0;
    margin: 0;
  }

  .activity-row .row-ids {
    margin-left: 30px;
  }

  .row-ids div {
    min-width: 0;
    display: grid;
    grid-column: 1 / -1;
    grid-template-columns: subgrid;
    align-items: center;
  }

  .row-ids dt {
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    text-transform: uppercase;
  }

  .row-ids dd {
    min-width: 0;
    margin: 0;
  }

  .row-ids code {
    min-width: 0;
    overflow: hidden;
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .activity-detail {
    min-width: 0;
    max-height: 320px;
    overflow: auto;
    margin: 0;
    padding: 8px 10px;
    border: 1px solid color-mix(in srgb, var(--border) 78%, transparent);
    border-radius: var(--radius-md);
    background: var(--surface-0);
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    line-height: 1.45;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  /* The language tag is absolutely positioned, so its containing block must not
     be the scroller — laid out in scrolled coordinates it would ride the content
     out of view. The wrapper positions it instead; min-width: 0 keeps the grid
     column off the code's min-content width, which the scroller no longer
     reports for itself. */
  .script-detail-wrap {
    position: relative;
    min-width: 0;
  }

  .activity-script-detail {
    min-width: 0;
    max-height: 320px;
    overflow: auto;
    margin: 0 0 0 30px;
    padding: 34px 12px 12px;
    border: 1px solid var(--code-block-border);
    border-radius: var(--radius-md);
    background: var(--code-block-bg);
    color: var(--code-block-text);
    box-shadow: var(--code-block-shadow);
    font-family: var(--font-mono);
    font-size: var(--font-md);
    line-height: 1.55;
    tab-size: 2;
    white-space: pre;
  }

  /* app.css draws the language tag; this only says where to put it. */
  .activity-script-detail::before {
    position: absolute;
    top: 9px;
    right: 10px;
  }

  .activity-script-detail code {
    font: inherit;
  }

  .approval-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }

  .approval-error {
    max-height: 6lh;
    margin: 0;
    min-width: 0;
    overflow-y: auto;
    color: var(--error);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
  }

  .replay-strip {
    position: absolute;
    top: 0;
    right: 0;
    left: 0;
    z-index: 2;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-sm);
    padding: var(--gap-xs) 12px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-0);
  }

  .replay-strip .kicker {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .approval-note {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .pending-stack {
    grid-area: pending;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    align-content: start;
    gap: 10px;
    margin: 0 clamp(18px, 5vw, 72px) 10px;
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .agent-chat.embedded .pending-stack {
    margin: 0 12px 10px;
  }

  /* No outer frame. The chip names the gate; each card is one call. A tinted
     section around that mixed two objects into one box, which is what made a
     single approval look like chrome stacked on chrome. */
  .pending-approvals {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 10px;
  }

  .pending-approval-card {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 10px;
    padding: 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-0);
  }

  .pending-approval-head {
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
    align-items: baseline;
    justify-content: space-between;
  }

  .pending-approval-head strong {
    min-width: 0;
    overflow: hidden;
    color: var(--text-1);
    font-size: var(--font-lg);
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pending-approval-head span {
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    line-height: 1.4;
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
    border-radius: var(--radius-chip);
    background: var(--text-3);
    animation: pulse 900ms var(--ease-in-out) infinite;
  }

  .thinking span:nth-child(2) {
    animation-delay: 120ms;
  }

  .thinking span:nth-child(3) {
    animation-delay: 240ms;
  }

  /* Opacity only. A dot that also hops draws the eye to the fact that the
     agent is thinking, which is the least interesting thing on the screen. */
  @keyframes pulse {
    0%, 80%, 100% { opacity: 0.35; }
    40% { opacity: 1; }
  }

  .error-banner {
    grid-area: banner;
    max-height: 6lh;
    overflow-y: auto;
    overflow-wrap: anywhere;
    margin: 0 clamp(18px, 5vw, 72px) 10px;
    padding: 8px 10px;
    border: 1px solid color-mix(in srgb, var(--error) 35%, var(--border));
    border-radius: var(--radius-md);
    color: var(--error);
    background: color-mix(in srgb, var(--error) 9%, var(--surface-1));
    font-size: var(--font-md);
  }

  .agent-chat.embedded .error-banner {
    margin: 0 12px 10px;
  }

  .closed-banner {
    grid-area: banner;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0 clamp(18px, 5vw, 72px) 10px;
    padding: 8px 10px;
    border: 1px solid color-mix(in srgb, var(--success) 30%, var(--border));
    border-radius: var(--radius-md);
    color: var(--text-2);
    background: color-mix(in srgb, var(--success) 8%, var(--surface-1));
    font-size: var(--font-md);
    font-weight: 600;
  }

  .closed-banner :global(svg) {
    flex: 0 0 auto;
    color: var(--success);
  }

  .agent-chat.embedded .closed-banner {
    margin: 0 12px 10px;
  }

  .composer-wrap {
    grid-area: composer;
    position: relative;
    margin: 0 clamp(18px, 5vw, 72px) 18px;
  }

  .agent-chat.embedded .composer-wrap {
    margin: 0 12px 12px;
  }

  .handler-picker {
    position: absolute;
    right: 0;
    bottom: calc(100% + 8px);
    left: 0;
    z-index: 12;
    display: grid;
    gap: 6px;
    max-height: 300px;
    overflow-y: auto;
    padding: 8px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--surface-1) 96%, var(--surface-0));
    box-shadow: var(--shadow-dropdown);
  }

  .picker-hint {
    margin: 0 2px 2px;
    color: var(--text-3);
    font-size: var(--font-sm);
    font-weight: 680;
    letter-spacing: 0.02em;
  }

  .picker-row {
    min-width: 0;
    display: grid;
    gap: 3px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    color: var(--text-1);
    text-align: left;
    cursor: pointer;
    font: inherit;
  }

  .picker-row:focus-visible,
  .picker-row.active {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
    background: color-mix(in srgb, var(--accent) 9%, var(--surface-2));
    outline: 0;
  }

  @media (hover: hover) and (pointer: fine) {
    .picker-row:hover {
      border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
      background: color-mix(in srgb, var(--accent) 9%, var(--surface-2));
    }
  }

  .picker-row.target {
    border-color: color-mix(in srgb, var(--model) 36%, var(--border));
    background: color-mix(in srgb, var(--model) 13%, var(--surface-2));
  }

  .picker-row strong {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-md);
    font-weight: 680;
  }

  .picker-row small {
    display: -webkit-box;
    overflow: hidden;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.4;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
  }

  .picker-name {
    min-width: 0;
    display: flex;
    gap: 7px;
    align-items: center;
  }

  /* mid_turn badge: neutral for enqueue, accent-positive for a join, warning for one that
     needs an idle agent — so "what will sending do right now" reads at a glance. */
  .mid-turn {
    flex: 0 0 auto;
    padding: 1px 7px;
    border: 1px solid var(--border);
    border-radius: var(--radius-chip);
    background: var(--surface-3);
    color: var(--text-3);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.03em;
    text-transform: uppercase;
  }

  .mid-turn.accept {
    border-color: color-mix(in srgb, var(--success) 32%, var(--border));
    background: color-mix(in srgb, var(--success) 15%, var(--surface-2));
    color: color-mix(in srgb, var(--success) 82%, var(--text-1));
  }

  .mid-turn.reject {
    border-color: color-mix(in srgb, var(--warning) 32%, var(--border));
    background: color-mix(in srgb, var(--warning) 15%, var(--surface-2));
    color: color-mix(in srgb, var(--warning) 82%, var(--text-1));
  }

  .composer-toolbar {
    display: flex;
    gap: var(--gap-sm);
    align-items: center;
    margin-bottom: var(--gap-sm);
  }

  /* The handler chip gives way first, truncating its target; the rest keep their words. */
  .composer-toolbar :global(.chip) {
    flex-shrink: 0;
  }

  .composer-toolbar :global(.target-chip) {
    flex-shrink: 1;
  }

  .composer-toolbar :global(.stop-chip) {
    margin-left: auto;
  }

  .target-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .handler-form {
    padding: 12px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-md);
    background: var(--surface-1);
  }

  .form-error {
    margin: 0 0 9px;
    color: var(--error);
    font-size: var(--font-sm);
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
  }

  .form-send {
    padding: 6px 13px;
    border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--accent) 16%, var(--surface-2));
    color: var(--text-1);
    font: inherit;
    font-size: var(--font-md);
    font-weight: 680;
    cursor: pointer;
  }

  @media (hover: hover) and (pointer: fine) {
    .form-send:hover:not(:disabled) {
      background: color-mix(in srgb, var(--accent) 26%, var(--surface-2));
    }
  }

  .form-send:disabled {
    cursor: default;
    opacity: var(--disabled-opacity);
  }

  .composer {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
    padding: 8px 8px 8px 12px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-md);
    background: var(--surface-1);
    color: var(--text-3);
  }

  /* The same token the send button beside it is built from, so the pair matches:
     IconButton is --control-height, and this was left on a literal 32px when the
     button adopted it. The drift checker cannot catch this one — it only looks at
     heights near a `cursor: pointer`, and a text field has none. */
  .composer input {
    min-width: 0;
    height: var(--control-height);
    border: 0;
    outline: none;
    background: transparent;
    color: var(--text-1);
    font-size: var(--font-lg);
  }

  .composer input::placeholder {
    color: var(--text-3);
  }

  .composer input:disabled {
    opacity: var(--disabled-opacity);
  }

  .composer.closed {
    border-color: color-mix(in srgb, var(--success) 24%, var(--border));
    background: color-mix(in srgb, var(--surface-1) 80%, var(--surface-0));
  }

  @media (max-width: 980px) {
    .chat-shell {
      border-right: 0;
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

  @media (prefers-reduced-motion: reduce) {
    /* The busy affordance stays; only the movement goes. */
    .thinking span {
      animation: none;
      opacity: 0.85;
    }

    /* Chevrons still end up rotated — they just stop swinging there. */
    .activity-row-chevron {
      transition: none;
    }
  }
</style>
