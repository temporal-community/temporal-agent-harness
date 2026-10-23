<script module lang="ts">
  export interface DisclosureSeed {
    feeds: Record<string, boolean>;
    groups: string[];
    thoughts: string[];
  }

  /* The next instance starts here, then the seed is forgotten. A render can show the
     state a click would leave without sharing it with any other feed. */
  let disclosureSeed: DisclosureSeed | null = null;

  export function seedDisclosure(seed: DisclosureSeed | null): void {
    disclosureSeed = seed;
  }

  function takeDisclosureSeed(): DisclosureSeed | null {
    const seed = disclosureSeed;
    disclosureSeed = null;
    return seed;
  }

  /** One feed, or one nested group inside it. The separator is not part of either id. */
  export function groupKey(feedId: string, groupId: string): string {
    return `${feedId}\u001f${groupId}`;
  }
</script>

<script lang="ts">
  import {
    ArrowDown,
    ArrowUp,
    Check,
    ChevronDown,
    ChevronRight,
    CheckCircle2,
    CircleAlert,
    CircleX,
    ClipboardList,
    Cpu,
    MessageCircle,
    MessageSquareText,
    Plus,
    RotateCw,
    ShieldCheck,
    SlidersHorizontal,
    Sparkles,
    SquareCode
  } from "@lucide/svelte";
  import { createVirtualizer } from "@tanstack/svelte-virtual";
  import { tick, untrack } from "svelte";
  import type { Attachment } from "svelte/attachments";
  import type {
    AgentInboundMessage,
    AgentInterfaceFunction,
    FileCitationAnnotation,
    JsonRecord,
    MidTurn,
    Session
  } from "$lib/api/types";
  import { approvalAlreadyResolved } from "$lib/api/httpClient";
  import { tokenFigure, type ContextWindowUsage } from "$lib/cost/pricing";
  import { dissolveClear, inputClearDissolve } from "$lib/motion/inputClearDissolve";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import LongContent from "$lib/components/primitives/LongContent.svelte";
  import { windowedFocus } from "$lib/components/primitives/windowedFocus";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import StateGlyph from "$lib/components/primitives/StateGlyph.svelte";
  import StatusChip from "$lib/components/primitives/StatusChip.svelte";
  import TraceImages from "$lib/components/primitives/TraceImages.svelte";
  import TraceRow from "$lib/components/primitives/TraceRow.svelte";
  import { codeModeScript } from "$lib/state/codeModeNesting";
  import { dismissable } from "$lib/state/dismissable.svelte";
  import { formatLogValue } from "$lib/state/logValue";
  import { requestKey } from "$lib/state/pendingRequests";
  import { toolImages } from "$lib/state/toolImages";
  import { NO_THOUGHT_SUMMARY, foldTurnThought, type TurnThought } from "$lib/state/thoughtSummary";
  import {
    formatElapsedDuration,
    formatTimestamp,
    rowIdentifiers,
    type ReplayLogRow
  } from "$lib/state/replayLog";
  import {
    replyAnnouncement,
    replyLabel,
    settledSummary,
    stepHasDetails,
    traceSteps,
    turnProgress,
    type ReplyPhase,
    type TraceStep
  } from "$lib/state/replyStatus";
  import { STEP_GLYPHS } from "$lib/state/logTree";
  import {
    choiceField,
    choiceKind,
    choiceLabel,
    choicePayload,
    choiceText,
    composerHandler,
    takesScript,
    type ChoiceKind
  } from "$lib/state/handlerRouting";
  import { singleStringPayload } from "$lib/state/inboundMessageText";
  import type { TranscriptItem } from "$lib/state/transcript";
  import type { InterfaceStatus } from "$lib/state/agentRun.svelte";
  import JsonReply from "$lib/components/chat/JsonReply.svelte";
  import MarkdownMessage, { markdownBlocks, markdownToText } from "$lib/components/chat/MarkdownMessage.svelte";
  import ContextUsage from "./ContextUsage.svelte";
  import HandlerActionCard from "./HandlerActionCard.svelte";
  import PendingBannerStack from "./PendingBannerStack.svelte";
  import PendingCallbackCard, { DECLINED, type CallbackOutcome } from "./PendingCallbackCard.svelte";
  import PendingClientToolCard from "./PendingClientToolCard.svelte";
  import PendingCard from "./PendingCard.svelte";
  import type { ClientToolStatus } from "$lib/state/clientToolProvider";
  import ReplyStatus from "./ReplyStatus.svelte";
  import {
    singleStringField,
    type SchemaField
  } from "@temporalio/agent-harness-client";
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
  /** How the selected handler is written in the text box: prose or a script. */
  type HandlerKind = "text" | "script";
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
    /** Which agent the composer is addressing (null = the session's parent). */
    messageTargetWorkflowId?: string | null;
    currentAgentWorkflowType?: string | null;
    connecting?: boolean;
    sending?: boolean;
    creatingSession?: boolean;
    closed?: boolean;
    error?: string | null;
    /** Send a message, optionally to a subagent rather than the session's parent. Resolving
     * false means it was not sent, and the composer keeps what the user typed. */
    onSend?: (
      message: AgentInboundMessage,
      workflowId?: string | null
    ) => void | boolean | Promise<void | boolean>;
    /** Open the new-session setup for this agent, offered once the session has ended. */
    onStartNewSession?: () => void;
    /** Ask an agent for its accepted messages again, after the lookup failed. */
    onRetryInterface?: (workflowId: string) => void | Promise<void>;
    onApproveTool?: (
      workflowId: string,
      toolId: string,
      approved: boolean,
      remember?: boolean,
      /** Told to the agent with a rejection; the run says "Rejected in chat." without one. */
      reason?: string
    ) => void | Promise<void>;
    /** Fulfill a pending callback tool call (e.g. answer an `ask_user` question). */
    onCallbackResult?: (
      workflowId: string,
      toolId: string,
      outcome: CallbackOutcome
    ) => void | Promise<void>;
    clientToolStatuses?: Readonly<Record<string, ClientToolStatus>>;
    onRetryClientTool?: (toolId: string) => void;
    editorScript?: string;
    onOpenScript?: () => void;
    /** The session agent's context window fill; null while the window size is unknown. */
    contextUsage?: ContextWindowUsage | null;
  }

  interface ChatMessage {
    id: string;
    role: "user" | "assistant";
    turnNumber?: number;
    text: string;
    timestamp: number;
    citations: FileCitationAnnotation[];
    /** A user message sent as a `script` payload, drawn as code rather than markdown. */
    script?: boolean;
    streaming?: boolean;
    data?: boolean;
    json?: unknown;
    error?: string;
  }

  interface TurnActivitySummary {
    label: string;
    detail: string;
    duration: string | null;
    endedAt: number;
    /** Gated calls that never ran, each kind once and only when there were any. */
    refusals: { word: "rejected" | "declined"; count: number }[];
  }

  interface ComposerAttachment {
    id: string;
    name: string;
    type: string;
    size: number;
    dataUrl: string;
  }

  interface AttachmentInput {
    field: string;
    multiple: boolean;
    accept: string;
    encoding: "base64" | "uri" | "parts";
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
    messageTargetWorkflowId = $bindable(null),
    currentAgentWorkflowType = null,
    connecting = false,
    sending = false,
    creatingSession = false,
    closed = false,
    error = null,
    onSend,
    onStartNewSession,
    onRetryInterface,
    onApproveTool,
    onCallbackResult,
    clientToolStatuses = {},
    onRetryClientTool,
    editorScript = "",
    onOpenScript,
    contextUsage = null
  }: Props = $props();
  let draft = $state("");
  let composerInput = $state<HTMLTextAreaElement | null>(null);
  let composerExpanded = $state(false);
  let historyIndex = $state(-1);
  let historyStash = $state("");
  let localMessages = $state<ChatMessage[]>([]);
  let observedSessionId = $state<string | null>(null);
  /* A feed is one message's trace. Turn numbers are not unique per feed: one turn holds
     several messages, and a continue-as-new reuses them. The reader's choice is kept on
     the message. A trace is open while its turn works, and once it settles if a step
     failed, was rejected or is waiting; otherwise it is closed until they open it. */
  const seededDisclosure = takeDisclosureSeed();
  let activityOverrides = $state<Record<string, boolean>>({ ...(seededDisclosure?.feeds ?? {}) });
  let expandedLogRows = $state<string[]>([...(seededDisclosure?.groups ?? [])]);
  /* The list re-renders as rows arrive. The key is this feed plus the turn, so an open
     thought survives that and stays shut in every other feed. */
  let openThoughts = $state<string[]>([...(seededDisclosure?.thoughts ?? [])]);
  let resolvingApprovalIds = $state<string[]>([]);
  let approvalErrors = $state<Record<string, string>>({});
  let decidedApprovalIds = $state<string[]>([]);
  /* By row: null until "Reject with a reason" opens the field. */
  let rejectReasons = $state<Record<string, string | null>>({});
  let messageListElement = $state<HTMLDivElement | null>(null);
  /* The attention queue's folded height, which the end of the message list keeps clear. */
  let queueHead = $state(0);
  /* Its whole height, open or folded, which "Jump to latest" rides above. */
  let queueCover = $state(0);
  /* The message list's scrollbar width, which the queue laid over the list keeps clear. */
  let listGutter = $state(0);
  /* Whether the view follows the tail: true while the reader is at the end, false once
     they scroll up, true again when they come back. `unseen` is what arrived meanwhile. */
  let following = $state(true);
  let unseen = $state(false);
  let pickerSelectionIndex = $state(0);
  let pickerSignature = $state("");
  let pickerButtonOpen = $state(false);
  let dismissedPickerDraft = $state<string | null>(null);
  let selectedHandlers = $state<Record<string, string>>({});
  let sentChoices = $state<Record<string, { value: string; after: number }>>({});
  let openChoiceName = $state<string | null>(null);
  let choiceIndex = $state(0);
  let choiceSending = $state(false);
  let attachments = $state<ComposerAttachment[]>([]);
  let attachmentError = $state<string | null>(null);
  let attachmentInputElement = $state<HTMLInputElement | null>(null);

  /* Transcript items keep their identity until they change, so their messages can too:
     an unchanged message is the same object, and the keyed list leaves it alone. */
  const messageOfItem = new WeakMap<TranscriptItem, ChatMessage>();

  const transcriptMessages = $derived(seedMessages(items));
  const messages = $derived([...transcriptMessages, ...localMessages]);

  /* Only the messages in view are in the DOM: chat was 97% of a 5,000-message page, and
     every layout read anywhere on it paid for all of them. A message and the activity
     drawn under it are one item, measured as drawn. */
  /* How near the end still counts as at it: a trackpad's last flick rarely lands on 0. */
  const FOLLOW_SLACK = 48;
  const messageRows = createVirtualizer<HTMLDivElement, HTMLElement>({
    /* Seeded here as well as in the effect below, which SSR never runs. */
    count: untrack(() => messages.length),
    getItemKey: (index) => messages[index]?.id ?? index,
    estimateSize: () => 180,
    getScrollElement: () => messageListElement,
    initialRect: { width: 0, height: 800 },
    overscan: 4,
    /* Held at the end while the reader is there: a streaming reply grows its row, and a
       new message is appended, without carrying the view away from it. */
    anchorTo: "end",
    followOnAppend: true,
    scrollEndThreshold: FOLLOW_SLACK
  });
  /* Untracked: setOptions publishes the store, which would re-run the effect calling it. */
  const messageList = () => untrack(() => $messageRows);
  const measureMessage: Attachment<HTMLElement> = (node) => messageList().measureElement(node);
  const remeasureMessages = () => messageList().measure();
  const messageFocus = windowedFocus(
    (index) => messageList().scrollToIndex(index, { align: "auto" }),
    () => messages.length
  );

  const liveMessages = $derived(
    liveItems ? [...seedMessages(liveItems), ...localMessages] : messages
  );
  const runLogs = $derived(liveLogs ?? logs);
  const startedTurns = $derived(
    runLogs.filter((row) => row.event === "turn_started").map((row) => row.turnNumber)
  );
  const queuedMessages = $derived(
    runLogs.filter(
      (row) =>
        row.event === "message_accepted" &&
        row.actor === "queue" &&
        !startedTurns.includes(row.turnNumber)
    )
  );
  const queuedMessageTexts = $derived(
    queuedMessages.map((message) => markdownToText(message.body || "Queued message"))
  );
  const queuedMessagePreview = $derived.by(() => {
    const firstLine = queuedMessageTexts[0]?.split(/\r?\n/, 1)[0]?.trim() ?? "";
    return `${firstLine.charAt(0).toUpperCase()}${firstLine.slice(1)}`;
  });
  const queuedMessageTip = $derived(queuedMessageTexts.join(" • "));
  const latestScriptUpdateTurn = $derived(
    runLogs.findLast(
      (row) =>
        row.event === "callback_requested" &&
        (row.toolName === "write_code" || row.toolName === "edit_code")
    )?.turnNumber ?? null
  );
  const editorLineCount = $derived(editorScript ? editorScript.split("\n").length : 0);
  const viewRowIds = $derived(new Set(logs.map((row) => row.id)));
  const sentUserMessages = $derived(
    liveMessages
      .filter((message) => message.role === "user")
      .map((message) => message.text)
  );
  const logsByTurn = $derived(groupLogsByTurn(logs));
  /* Every timed row, shown or not: what a turn's duration and tokens are read from. */
  const timedLogsByTurn = $derived(
    Map.groupBy(
      logs.filter((row) => row.timestamp > 0),
      (row) => row.turnNumber
    )
  );
  /* Every row, timed or not: a turn_end stamped 0 still ends its turn. */
  const rowsByTurn = $derived(Map.groupBy(logs, (row) => row.turnNumber));
  const latestTurn = $derived(logs.reduce((max, row) => Math.max(max, row.turnNumber), 0));
  const latestProgress = $derived(turnProgress(rowsByTurn.get(latestTurn) ?? []));
  /* As of the view: a cursor parked mid-turn reads that turn as running, timed to the cursor. */
  const turnRunning = $derived(!closed && latestTurn > 0 && latestProgress.phase.kind !== "done");
  /* Live, a sent message is pending before its turn has published anything. */
  const replyPending = $derived(turnRunning || (live && sending && !closed));
  const pendingPhase = $derived<ReplyPhase>(turnRunning ? latestProgress.phase : { kind: "thinking" });
  const eventNow = $derived(
    logs.findLast((row) => Number.isFinite(row.timestamp) && row.timestamp > 0)?.timestamp ?? null
  );
  const replyAnnounced = $derived(
    replyPending
      ? replyAnnouncement(pendingPhase)
      : latestProgress.phase.kind === "done" && latestTurn > 0
        ? replyAnnouncement(latestProgress.phase)
        : ""
  );

  /* The in-flight turn's status rides its header, under the message that started it. */
  const statusInHeader = $derived(
    turnRunning && messages.some((message) => message.role === "user" && message.turnNumber === latestTurn)
  );
  /* Until that message is drawn — a send not yet accepted — the same header stands in room the
     list keeps at its end, so following the tail and "Jump to latest" land below it. */
  const REPLY_SLOT = 40;
  const statusAtEnd = $derived(replyPending && !statusInHeader);
  const latestReplyIndex = $derived(messages.findLastIndex((message) => message.role === "assistant"));
  const resolvedApprovalKeys = $derived(resolvedApprovalIds(runLogs));
  /* Cleared by `callback_resolved` whoever fulfilled it — this pane, another tab, or a CLI. */
  const resolvedCallbackKeys = $derived(
    new Set(
      runLogs
        .filter((row) => row.event === "callback_resolved")
        .map((row) => approvalKey(row))
    )
  );
  /* Approvals and callbacks in one queue, in the order the agent asked for them. */
  const pendingRows = $derived(
    runLogs.filter(
      (row) =>
        isApprovalPending(row) ||
        (row.event === "callback_requested" &&
          approvalKey(row) != null &&
          !resolvedCallbackKeys.has(approvalKey(row)))
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
  /* A pick-from-a-list handler is a menu in the control row; the rest are the composer's. */
  const choiceHandlers = $derived(
    handlers.flatMap((handler) => {
      const field = choiceField(handler);
      if (!field) return [];
      const value = currentChoice(handler, field);
      const label = choiceLabel(handler, field);
      const icon = choiceIcon(choiceKind(handler, field));
      const tip = `${label}: ${value == null ? "not set" : choiceText(value)}`;
      return [{ handler, field, value, label, icon, tip }];
    })
  );
  const composerHandlers = $derived(handlers.filter((handler) => !choiceField(handler)));
  const openChoice = $derived(choiceHandlers.find((choice) => choice.handler.name === openChoiceName) ?? null);
  const handlerSelectionKey = $derived(`${sessionId}:${activeTarget?.workflowId ?? ""}`);
  const selectedHandlerName = $derived(selectedHandlers[handlerSelectionKey] ?? null);
  /* Typing goes to a handler written in the text box; one that takes a form is a card in the
     attention queue, so the box is always a message field. */
  const textHandlers = $derived(composerHandlers.filter((handler) => textFieldOf(handler) != null));
  const formHandlers = $derived(composerHandlers.filter((handler) => textFieldOf(handler) == null));
  const selectedHandler = $derived(composerHandler(textHandlers, selectedHandlerName));
  /* A handler other than the one plain typing goes to keeps its icon revealed, so the mode shows. */
  const offDefaultHandler = $derived(
    selectedHandler != null && selectedHandler.name !== composerHandler(textHandlers, null)?.name
  );
  const attachmentInput = $derived(
    selectedHandler ? attachmentInputForSchema(selectedHandler.parameters) : null
  );
  /** The field the text box fills for the selected handler. */
  const textFieldName = $derived(selectedHandler ? textFieldOf(selectedHandler) : null);
  const scriptInput = $derived(selectedHandler ? takesScript(selectedHandler) : false);
  const canSendToTarget = $derived(
    !closed && activeTarget != null && !activeTarget.closed && handlers.length > 0
  );
  /* The card's handler: a picked form handler, else, when nothing is written in the text box,
     the first that takes input. */
  const actionHandler = $derived(
    formHandlers.find((handler) => handler.name === selectedHandlerName) ??
      (textHandlers.length === 0 ? composerHandler(formHandlers, null) : null)
  );
  /* The run says what it accepts, never what it expects next. With no turn in flight in the
     live run (not the replay cursor's view), the agent can only be waiting on the reader, so
     that is when the card is offered; it claims nothing about whose turn a game is. */
  const runTurn = $derived(runLogs.reduce((max, row) => Math.max(max, row.turnNumber), 0));
  const runTurnRows = $derived(runLogs.filter((row) => row.turnNumber === runTurn));
  const runIdle = $derived(runTurn === 0 || turnProgress(runTurnRows).phase.kind === "done");
  /* One card per idle stretch: each turn's is a new request, so sending one drops it from the
     queue like an answer and the next idle run deals a fresh one. */
  const actionRow = $derived.by<ReplayLogRow | null>(() => {
    if (!actionHandler || !canSendToTarget || !runIdle) return null;
    const timestamp = runTurnRows.at(-1)?.timestamp ?? 0;
    return {
      id: `action:${activeTarget?.workflowId ?? sessionId}:${runTurn}`,
      index: -1,
      ordinal: -1,
      turnNumber: runTurn,
      sourceTurnNumber: runTurn,
      turnId: "",
      timestamp,
      event: "turn_end",
      actor: "user",
      tone: "neutral",
      label: actionHandler.name,
      citations: []
    };
  });
  const actionWhen = $derived(
    runTurn > 0 ? `After turn ${runTurn} · ${formatTimestamp(actionRow?.timestamp ?? 0)}` : "Before the first turn"
  );
  /* Requests the agent is blocked on first, in its order; the offered input after them. */
  const queueRows = $derived(actionRow ? [...pendingRows, actionRow] : pendingRows);
  /* What the end of the message list keeps clear: the queue's header plus its padding above
     and gap below, 8px and 10px in its CSS. An action card alone answers the reply above it,
     so the list keeps all of it clear, not only the header a blocked request leaves room for. */
  const queueReserve = $derived(
    queueHead === 0 ? 0 : actionRow && pendingRows.length === 0 ? queueCover : queueHead + 18
  );
  /* Pre, so a shrunk list's count lands before the rows render and index past its end. It runs
     as the component is set up, so it comes after everything it reads. */
  $effect.pre(() => {
    const count = messages.length;
    const paddingEnd = statusAtEnd ? REPLY_SLOT : 0;
    messageListElement;
    messageList().setOptions({ count, paddingEnd, scrollPaddingEnd: queueReserve });
  });
  const composerPlaceholder = $derived.by(() => {
    if (closed) return "Session ended";
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
    // Ready to type. The agent is in the top nav; policy and model are on their icons.
    return scriptInput ? "Python script" : "Message";
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
  const pickerDraft = $derived(
    pickerButtonOpen ? { name: "", rest: "" } : parsePickerDraft(draft)
  );
  /**
   * Typing `/` as the first character opens the handler picker. This is purely a client-side
   * convention for a familiar affordance — the harness has no notion of a slash, and the rows
   * are just whatever `agent_interface` returned.
   */
  const typedPickerOpen = $derived(
    draft.trimStart().startsWith("/") && dismissedPickerDraft !== draft
  );
  const pickerOpen = $derived(
    canSendToTarget && (typedPickerOpen || pickerButtonOpen) && !composerDisabled
  );
  const pickerItems = $derived(
    buildPickerItems(pickerOpen, targets, composerHandlers, pickerDraft, showTargetPicker)
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
  /* Nothing takes typed text: the box still takes it, keeps it, and says where input goes. */
  const textRouteNote = $derived(
    !canSendToTarget || selectedHandler || formHandlers.length === 0
      ? ""
      : actionRow
        ? `Use the card above to send ${actionHandler!.name}.`
        : "This agent takes its input on a card, offered above when it is not busy."
  );
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
      queueRows.length,
      queueReserve,
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
      pickerButtonOpen = false;
      sentChoices = {};
      openChoiceName = null;
      dismissedPickerDraft = null;
      attachments = [];
      attachmentError = null;
      localMessages = [];
      activityOverrides = {};
      expandedLogRows = [];
      openThoughts = [];
      following = true;
      unseen = false;
    }
  });

  /* What the virtualizer's end anchoring cannot see — the thinking bubble, a new session,
     a cursor move that changes no count — is followed here, and only while following. */
  $effect(() => {
    chatScrollSignature;
    if (!untrack(() => following)) {
      unseen = true;
      return;
    }
    void tick().then(() => messageList().scrollToEnd());
  });

  function trackFollowing(): void {
    const box = messageListElement;
    if (!box) return;
    /* Read from the box, not `isAtEnd()`: this listener can run before the virtualizer's
       own and see its offset a scroll behind. Same distance, same threshold. */
    following = box.scrollHeight - box.scrollTop - box.clientHeight <= FOLLOW_SLACK;
    if (following) unseen = false;
  }

  /* The list's end padding grows when requests start waiting on the reader, which no scroll
     event reports; a reader following the tail keeps the latest message in view. The rows grow as
     a reply streams or a trace opens, and the virtualizer's own end anchoring holds to its
     rows rather than the scroller's padded end, which is short by the status line. */
  const followOnResize: Attachment<HTMLElement> = (box) => {
    const observer = new ResizeObserver(() => {
      listGutter = box.offsetWidth - box.clientWidth;
      if (untrack(() => following)) messageList().scrollToEnd();
    });
    observer.observe(box);
    const rows = box.querySelector(".message-rows");
    if (rows) observer.observe(rows);
    return () => observer.disconnect();
  };

  function jumpToLatest(): void {
    following = true;
    unseen = false;
    messageList().scrollToEnd();
  }

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

  // Attachments belong to the handler they were added for. Only a pick writes
  // `selectedHandlers`: a default stored here would outlive the surface it was computed from.
  let resolvedHandlerName: string | null = null;
  $effect(() => {
    const resolved = selectedHandler?.name ?? null;
    if (resolved !== resolvedHandlerName) {
      resolvedHandlerName = resolved;
      attachments = [];
      attachmentError = null;
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
      if (item.kind === "user") emittedUsers.add(item.turnNumber);
      else if (item.kind !== "agent" || !emittedUsers.has(item.turnNumber)) continue;
      let message = messageOfItem.get(item);
      if (!message) {
        message = chatMessage(item);
        messageOfItem.set(item, message);
      }
      messages.push(message);
    }

    return messages;
  }

  function chatMessage(item: Extract<TranscriptItem, { kind: "user" | "agent" }>): ChatMessage {
    if (item.kind === "user") {
      return {
        id: `chat-${item.id}`,
        role: "user",
        turnNumber: item.turnNumber,
        text: item.text,
        timestamp: item.timestamp,
        citations: [],
        script: item.field === "script"
      };
    }
    return {
      id: `chat-${item.id}`,
      role: "assistant",
      turnNumber: item.turnNumber,
      text: item.text,
      timestamp: item.timestamp,
      citations: item.citations,
      streaming: item.streaming,
      data: item.data,
      json: item.json,
      error: item.error
    };
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
      if (row.event !== "tool_approval_resolved") continue;
      const key = approvalKey(row);
      if (key) result.add(key);
    }
    return result;
  }

  function approvalWorkflowId(row: ReplayLogRow): string {
    return row.workflowId ?? sessionId;
  }

  function approvalKey(row: ReplayLogRow): string | null {
    return requestKey(row, sessionId);
  }

  function isApprovalPending(row: ReplayLogRow): boolean {
    if (row.event !== "tool_approval_requested") return false;
    const key = approvalKey(row);
    return key != null && !resolvedApprovalKeys.has(key);
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
    return timedLogsByTurn.get(turnNumber) ?? [];
  }

  function turnActivitySummary(
    turnNumber: number | undefined,
    visibleRows: ReplayLogRow[]
  ): TurnActivitySummary {
    const turnRows = allLogsForTurn(turnNumber);
    const rows = turnRows.length > 0 ? turnRows : visibleRows;
    const timestamps = rows.map((row) => row.timestamp).filter((value) => value > 0);
    const startedAt = timestamps.reduce((min, value) => Math.min(min, value), Infinity);
    const endedAt = timestamps.reduce((max, value) => Math.max(max, value), -Infinity);
    const durationMs =
      timestamps.length >= 2 && Number.isFinite(startedAt) && Number.isFinite(endedAt)
        ? Math.max(0, (endedAt - startedAt) * 1000)
        : 0;

    return {
      label: turnNumber == null ? "Turn" : `Turn ${turnNumber}`,
      /* A working turn's current call has not reported yet, so its sum is a floor, not a total. */
      detail: `total ${tokenFigure(turnTokens(rows), unreportedCalls(rows) + (turnWorking(turnNumber) ? 1 : 0))} tok`,
      duration: durationMs > 0 ? formatElapsedDuration(durationMs) : null,
      endedAt: Number.isFinite(endedAt) ? endedAt : visibleRows[visibleRows.length - 1]?.timestamp ?? 0,
      refusals: turnRefusals(rows)
    };
  }

  /* A turn whose only gated call was rejected ran no tool, so it read "Replied" as if nothing
     had been asked of anyone. */
  function turnRefusals(rows: ReplayLogRow[]): TurnActivitySummary["refusals"] {
    const counts = {
      rejected: rows.filter((row) => row.event === "tool_approval_resolved" && row.status !== "approved").length,
      declined: rows.filter((row) => row.event === "callback_resolved" && row.body === DECLINED).length
    };
    return (["rejected", "declined"] as const).filter((word) => counts[word] > 0).map((word) => ({ word, count: counts[word] }));
  }

  function turnTokens(rows: ReplayLogRow[]): number {
    return rows.reduce((sum, row) => sum + (row.usage?.total ?? 0), 0);
  }

  /** Model calls that ended without reporting usage, which leave the turn's sum unknown or a floor. */
  function unreportedCalls(rows: ReplayLogRow[]): number {
    return rows.filter((row) => row.event === "model_interaction_ended" && row.usage == null).length;
  }

  function turnWorking(turnNumber: number | undefined): boolean {
    return turnRunning && turnNumber === latestTurn;
  }

  /* A step that failed, was rejected or waits on someone. The trace draws it at full contrast,
     and a settled turn holding one stays open, so the reader never has to unfold it. */
  const signalStep = (step: TraceStep) => step.state === "failed" || step.state === "rejected" || step.state === "awaiting";

  function activityExpanded(feedId: string, turnNumber: number | undefined): boolean {
    const chosen = activityOverrides[feedId];
    if (chosen != null) return chosen;
    if (turnNumber == null) return false;
    return turnWorking(turnNumber) || traceSteps(logsForTurn(turnNumber)).some(signalStep);
  }

  function toggleActivity(feedId: string, turnNumber: number | undefined): void {
    activityOverrides = { ...activityOverrides, [feedId]: !activityExpanded(feedId, turnNumber) };
  }

  /* A settled turn in a few words; a working one shows its live status instead. */
  function turnHeadline(
    turnNumber: number | undefined,
    duration: string | null,
    thought: TurnThought | null
  ): string {
    const progress = turnProgress(rowsByTurn.get(turnNumber ?? 0) ?? []);
    const summary = settledSummary(progress.tools, thought?.seconds ?? 0);
    return duration ? `${summary} · ${duration}` : summary;
  }

  /* A reply that is one JSON or code block already offers its own copy, in its corner. JSON
     offers it only once parsed, so a data reply that failed partway keeps this one. */
  function copiesWhole(message: ChatMessage): boolean {
    if (!message.text.trim()) return false;
    if (message.role === "user") return !message.script;
    if (scriptError(message.text)) return false;
    if (message.data) return !message.streaming && message.json === undefined;
    const blocks = markdownBlocks(message.text, []);
    return !(blocks.length === 1 && /^ {0,3}(`{3,}|~{3,})/.test(blocks[0]!));
  }

  function scriptError(text: string): boolean {
    return /^Script error\s*\(/.test(text.trimStart());
  }

  /** "Script error (MontyTypingError: …\n…" → its first line, without the wrapper. */
  function scriptErrorHead(text: string): string {
    return text.trimStart().replace(/^Script error\s*\(/, "").split("\n", 1)[0]!.trim();
  }

  function setThoughtOpen(key: string, open: boolean): void {
    if (open === openThoughts.includes(key)) return;
    openThoughts = open
      ? [...openThoughts, key]
      : openThoughts.filter((item) => item !== key);
  }

  function thoughtLabel(thought: TurnThought): string {
    return thought.seconds > 0
      ? `Thought ${formatElapsedDuration(thought.seconds * 1000)}`
      : "Thought";
  }

  function toggleStep(feedId: string, stepId: string): void {
    const key = groupKey(feedId, stepId);
    expandedLogRows = expandedLogRows.includes(key)
      ? expandedLogRows.filter((item) => item !== key)
      : [...expandedLogRows, key];
  }

  function stepScript(step: TraceStep): string | null {
    for (const row of step.rows) {
      const script = logScript(row);
      if (script) return script;
    }
    return null;
  }

  /* A script host's one argument is its script, drawn above as code. */
  function stepArguments(step: TraceStep): Record<string, unknown> | null {
    const input = step.rows.find((row) => row.input)?.input;
    if (!input) return null;
    const { script: _script, ...rest } = input;
    return Object.keys(rest).length > 0 ? (codeModeScript(input) ? rest : input) : null;
  }

  function stepIdentifiers(step: TraceStep): ReturnType<typeof rowIdentifiers> {
    const ids = step.rows.flatMap(rowIdentifiers);
    return ids.filter((id, index) => ids.findIndex((other) => other.label === id.label) === index);
  }

  /* Every event the step folded, each with its time: the audit record the row summarizes. */
  function stepFullDetail(step: TraceStep): string {
    return step.rows
      .map((row) => `${formatTimestamp(row.timestamp)} · ${row.label}\n${logFullDetail(row)}`)
      .join("\n\n");
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
    event: Event,
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
      const reason = approved ? undefined : (rejectReasons[row.id] ?? undefined);
      await onApproveTool(approvalWorkflowId(row), toolId, approved, remember, reason);
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
    /* The current handler, typed `/` or not: a bare `/` and Enter must not land on whichever
       handler is declared first, which is often a script runner. */
    const current = items.findIndex((item) => item.kind === "handler" && isCurrentPickerItem(item));
    if (current >= 0) return current;
    const firstHandler = items.findIndex((item) => item.kind === "handler");
    return firstHandler >= 0 ? firstHandler : 0;
  }

  /** How a handler's mid-turn behavior reads as a badge in the picker. */
  function midTurnLabel(mode: AgentInterfaceFunction["mid_turn"]): string {
    if (mode === "enqueue") return "queues";
    if (mode === "accept") return "joins";
    return "needs idle";
  }

  /** What sending now will do, said beside the handler chip while a reply is under way. */
  function midTurnNote(mode: MidTurn): string {
    switch (mode) {
      case "enqueue":
        return "Sends after this reply";
      case "accept":
        return "Joins this reply";
      case "reject":
        return "Send once this reply ends";
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
    /* Opened from the button, the list keeps focus and now lists this target's handlers. */
    if (pickerButtonOpen) return;
    draft = "/";
    composerInput?.focus();
  }

  function selectHandler(handler: AgentInterfaceFunction): void {
    const keepDraft = pickerButtonOpen;
    pickerButtonOpen = false;
    pickHandler(handler.name);
    // A typed slash is a command draft; the chip is only a view over the current draft.
    if (!keepDraft) draft = "";
    if (textFieldOf(handler) != null) {
      composerInput?.focus();
      return;
    }
    /* A form handler is the queue's card: the reader lands in its first field. */
    void tick().then(() =>
      document.querySelector<HTMLElement>('.pending-card[aria-label^="Action:"] form :is(input, select, textarea)')?.focus()
    );
  }

  function pickHandler(name: string): void {
    selectedHandlers[handlerSelectionKey] = name;
  }

  /* The card's own switcher: the draft is the reader's, and focus stays on the switcher of the
     card dealt for the new handler. */
  function switchAction(name: string): void {
    actionMenuOpen = false;
    pickHandler(name);
    void tick().then(() =>
      document.querySelector<HTMLElement>('.pending-card[aria-label^="Action:"] .action-switch')?.focus()
    );
  }

  /* The action card's switcher opens the composer's handler rows under the card's title. */
  function toggleActionMenu(): void {
    actionMenuOpen = !actionMenuOpen;
    actionMenuIndex = Math.max(0, formHandlers.findIndex((handler) => handler.name === actionHandler?.name));
  }

  /* As the composer's list: arrows and Home/End move, Enter or Space picks, Tab leaves. The
     queue's Escape folds the whole stack, list and all. */
  function handleActionMenuKeydown(event: KeyboardEvent): void {
    const count = formHandlers.length;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      actionMenuIndex = (actionMenuIndex + (event.key === "ArrowDown" ? 1 : -1) + count) % count;
    } else if (event.key === "Home" || event.key === "End") {
      actionMenuIndex = event.key === "Home" ? 0 : count - 1;
    } else if (event.key === "Enter" || event.key === " ") {
      switchAction(formHandlers[actionMenuIndex]!.name);
    } else {
      if (event.key === "Tab") actionMenuOpen = false;
      return;
    }
    event.preventDefault();
  }

  /** The field typing fills for this handler, when it is written in the text box at all. */
  function textFieldOf(handler: AgentInterfaceFunction): string | null {
    return (
      singleStringField(handler.parameters) ??
      textFieldBesideAttachments(handler.parameters, attachmentInputForSchema(handler.parameters)?.field)
    );
  }

  function closePicker(): void {
    if (typedPickerOpen) dismissedPickerDraft = draft;
    /* Opened from the button, the dismissable layer hands focus back to it. */
    if (pickerButtonOpen) {
      pickerButtonOpen = false;
      return;
    }
    void tick().then(() => requestAnimationFrame(() => composerInput?.focus()));
  }

  function togglePicker(): void {
    if (pickerOpen) return closePicker();
    openChoiceName = null;
    pickerButtonOpen = true;
  }

  /* The handler list opened from its button holds focus, like the choice lists beside it.
     Escape is the dismissable layer's. */
  function handlePickerKeydown(event: KeyboardEvent): void {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      movePickerSelection(event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Home" || event.key === "End") {
      pickerSelectionIndex = event.key === "Home" ? 0 : Math.max(pickerItems.length - 1, 0);
    } else if (event.key === "Enter" || event.key === " ") {
      acceptPickerSelection();
    } else {
      if (event.key === "Tab") pickerButtonOpen = false;
      return;
    }
    event.preventDefault();
  }

  type ChoiceHandler = (typeof choiceHandlers)[number];

  function handlerIcon(kind: HandlerKind): typeof Cpu {
    switch (kind) {
      case "text":
        return MessageSquareText;
      case "script":
        return SquareCode;
      default: {
        const unhandled: never = kind;
        return unhandled;
      }
    }
  }

  function choiceIcon(kind: ChoiceKind): typeof Cpu {
    switch (kind) {
      case "policy":
        return ShieldCheck;
      case "model":
        return Cpu;
      case "other":
        return SlidersHorizontal;
      default: {
        const unhandled: never = kind;
        return unhandled;
      }
    }
  }

  function choiceKey(handler: AgentInterfaceFunction): string {
    return `${activeTarget?.workflowId ?? ""} ${handler.name}`;
  }

  /** The run log's last admitted message to this handler on this target, or -1. */
  function lastChoiceRow(handler: AgentInterfaceFunction): number {
    return runLogs.findLastIndex(
      (row) =>
        row.message?.handler === handler.name &&
        (row.workflowId ?? sessionId) === activeTarget?.workflowId
    );
  }

  /**
   * What this target was last sent for the handler, else its schema default; null if neither is a
   * choice. A value just sent stands until the run log admits a newer message to the handler, so
   * the button and its list show the pick at once.
   */
  function currentChoice(handler: AgentInterfaceFunction, field: SchemaField): string | null {
    const row = lastChoiceRow(handler);
    const local = sentChoices[choiceKey(handler)];
    const sent = local && local.after >= row ? local.value : runLogs[row]?.message?.payload[field.name];
    const value = sent ?? field.default;
    return typeof value === "string" && field.choices?.includes(value) ? value : null;
  }

  /* `sending` only stops a `reject` handler and spans the whole turn, so a choice send also waits
     on its own: one choice in flight at a time, across every choice button. */
  function choiceBlocked(choice: ChoiceHandler): boolean {
    return (
      choiceSending ||
      composerDisabled ||
      !canSendToTarget ||
      (sending && choice.handler.mid_turn === "reject")
    );
  }

  function toggleChoice(choice: ChoiceHandler): void {
    if (openChoiceName === choice.handler.name) {
      openChoiceName = null;
      return;
    }
    if (choiceBlocked(choice)) return;
    pickerButtonOpen = false;
    openChoiceName = choice.handler.name;
    /* No active row while unset, so Enter pressed through the opening press can't pick one. */
    choiceIndex = choice.field.choices!.indexOf(choice.value ?? "");
  }

  /** Send the picked option through the same path as the handler form. */
  async function pickChoice(choice: ChoiceHandler, option: string): Promise<void> {
    openChoiceName = null;
    if (option === choice.value || choiceBlocked(choice)) return;
    const key = choiceKey(choice.handler);
    const previous = sentChoices[key];
    sentChoices[key] = { value: option, after: lastChoiceRow(choice.handler) };
    choiceSending = true;
    let sent = false;
    try {
      const payload = choicePayload(choice.field, option);
      sent = await dispatchMessage(choice.handler, payload, summarizePayload(choice.handler.name, payload));
    } finally {
      choiceSending = false;
      if (!sent && sentChoices[key]?.value === option) {
        if (previous) sentChoices[key] = previous;
        else delete sentChoices[key];
      }
    }
  }

  function handleChoiceKeydown(event: KeyboardEvent, choice: ChoiceHandler): void {
    const options = choice.field.choices!;
    let next: number | null = null;
    if (event.key === "ArrowDown") next = choiceIndex + 1;
    else if (event.key === "ArrowUp") next = choiceIndex < 0 ? options.length - 1 : choiceIndex - 1;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = options.length - 1;
    else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (choiceIndex < 0) openChoiceName = null;
      else void pickChoice(choice, options[choiceIndex]!);
      return;
    } else if (event.key === "Tab") {
      openChoiceName = null;
      return;
    } else if (event.key.length === 1 && !event.altKey && !event.ctrlKey && !event.metaKey) {
      const key = event.key.toLowerCase();
      const ahead = [...options.slice(choiceIndex + 1), ...options.slice(0, choiceIndex + 1)];
      const match = ahead.find((option) => choiceText(option).toLowerCase().startsWith(key));
      if (match) next = options.indexOf(match);
    }
    if (next == null) return;
    event.preventDefault();
    choiceIndex = (next + options.length) % options.length;
  }

  /* Scrolls the list, the option's offsetParent, and nothing above it: scrollIntoView would
     slide the pane rail as well. */
  const keepInMenu = (active: boolean): Attachment<HTMLElement> => (node) => {
    const menu = node.parentElement;
    if (!active || !menu) return;
    const bottom = node.offsetTop + node.offsetHeight;
    if (node.offsetTop < menu.scrollTop) menu.scrollTop = node.offsetTop;
    else if (bottom > menu.scrollTop + menu.clientHeight) menu.scrollTop = bottom - menu.clientHeight;
  };

  const focusOnOpen: Attachment<HTMLElement> = (node) => node.focus();

  function schemaRecord(value: unknown): Record<string, unknown> | null {
    return typeof value === "object" && value != null && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  }

  /**
   * Attachment capability is schema-only. A top-level field qualifies when its name is
   * `attachments`, `files`, or `images`; it declares contentEncoding=base64,
   * contentMediaType, or format=binary|uri; or it is an array whose item object has a
   * `type` enum containing image or file. Nothing is inferred from handler names.
   */
  function attachmentInputForSchema(parameters: Record<string, unknown>): AttachmentInput | null {
    const properties = schemaRecord(parameters.properties);
    if (!properties) return null;

    for (const [field, value] of Object.entries(properties)) {
      const schema = schemaRecord(value);
      if (!schema) continue;
      const items = schemaRecord(schema.items);
      const itemProperties = schemaRecord(items?.properties);
      const typeSchema = schemaRecord(itemProperties?.type);
      const partTypes = Array.isArray(typeSchema?.enum) ? typeSchema.enum : [];
      const named = /^(attachments|files|images)$/i.test(field);
      const annotated =
        schema.contentEncoding === "base64" ||
        typeof schema.contentMediaType === "string" ||
        schema.format === "binary" ||
        schema.format === "uri";
      const parts = partTypes.some((type) => type === "image" || type === "file");
      if (!named && !annotated && !parts) continue;

      const accept =
        typeof schema.contentMediaType === "string"
          ? schema.contentMediaType
          : /^images$/i.test(field)
            ? "image/*"
            : "";
      return {
        field,
        multiple: schema.type === "array",
        accept,
        encoding:
          parts ? "parts" : schema.contentEncoding === "base64" ? "base64" : "uri"
      };
    }
    return null;
  }

  function textFieldBesideAttachments(
    parameters: Record<string, unknown>,
    attachmentField: string | undefined
  ): string | null {
    if (!attachmentField) return null;
    const properties = schemaRecord(parameters.properties);
    if (!properties) return null;
    const textFields = Object.entries(properties).filter(
      ([name, value]) => name !== attachmentField && schemaRecord(value)?.type === "string"
    );
    return textFields.length === 1 ? textFields[0]![0] : null;
  }

  function attachmentValue(input: AttachmentInput): unknown {
    const encoded = attachments.map((attachment) => {
      const base64 = attachment.dataUrl.slice(attachment.dataUrl.indexOf(",") + 1);
      if (input.encoding === "base64") return base64;
      if (input.encoding === "uri") return attachment.dataUrl;
      return {
        type: attachment.type.startsWith("image/") ? "image" : "file",
        name: attachment.name,
        mediaType: attachment.type,
        data: base64
      };
    });
    return input.multiple ? encoded : encoded[0];
  }

  const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;

  async function addFiles(files: FileList | File[]): Promise<void> {
    const input = attachmentInput;
    if (!input) return;
    attachmentError = null;
    const next = input.multiple ? Array.from(files) : Array.from(files).slice(0, 1);
    for (const file of next) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        attachmentError = `${file.name} is larger than 10 MB.`;
        continue;
      }
      if (
        input.accept &&
        !(input.accept === "image/*"
          ? file.type.startsWith("image/")
          : input.accept.split(",").map((type) => type.trim()).includes(file.type))
      ) {
        attachmentError = `${file.name} is not an accepted file type.`;
        continue;
      }
      let dataUrl: string;
      try {
        dataUrl = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(file);
        });
      } catch {
        attachmentError = `${file.name} could not be read.`;
        continue;
      }
      const item = {
        id: crypto.randomUUID(),
        name: file.name,
        type: file.type || "application/octet-stream",
        size: file.size,
        dataUrl
      };
      attachments = input.multiple ? [...attachments, item] : [item];
    }
  }

  function removeAttachment(id: string): void {
    attachments = attachments.filter((attachment) => attachment.id !== id);
    attachmentError = null;
  }

  function handleComposerDrop(event: DragEvent): void {
    if (!attachmentInput || !event.dataTransfer?.files.length) return;
    event.preventDefault();
    void addFiles(event.dataTransfer.files);
  }

  function handleComposerPaste(event: ClipboardEvent): void {
    if (!attachmentInput || !event.clipboardData?.files.length) return;
    event.preventDefault();
    void addFiles(event.clipboardData.files);
  }

  /**
   * Send the text box's contents to the selected handler's text field. The payload's field name
   * comes from the schema, so this works for `text`, `script`, `prompt`, or anything else. With
   * no handler written in the box nothing is sent and the text stays; a form handler takes its
   * input on its card. Text is cleared only once it is sent.
   */
  async function sendMessage(text = draft): Promise<void> {
    const handler = selectedHandler;
    const field = textFieldName;
    if (!text.trim() || text.trimStart().startsWith("/") || !handler || !field || !canSendToTarget || composerBusy) {
      return;
    }

    historyIndex = -1;
    const payload = singleStringPayload(field, text);
    if (attachmentInput && attachments.length > 0) {
      payload[attachmentInput.field] = attachmentValue(attachmentInput);
    }
    const sent = await dispatchMessage(handler, payload, text);
    if (sent) {
      // Do not clear a follow-up the user started while this send was in flight.
      if (draft === text) dissolveClear(composerInput, () => (draft = ""));
      attachments = [];
    }
  }

  /* A picked card stands, saying it was sent, until the turn it started is under way; then the
     pick goes, so the next idle run offers the default again. */
  let sentPick = $state<{ key: string; name: string } | null>(null);
  let actionMenuOpen = $state(false);
  let actionMenuIndex = $state(0);
  $effect(() => {
    if (runIdle || !sentPick) return;
    const { key, name } = sentPick;
    sentPick = null;
    untrack(() => {
      if (name === selectedHandlers[key]) delete selectedHandlers[key];
    });
  });

  /** Send a queue card's form to its handler. */
  async function sendAction(handler: AgentInterfaceFunction, payload: JsonRecord): Promise<boolean> {
    const sent = await dispatchMessage(handler, payload, summarizePayload(handler.name, payload));
    if (sent && untrack(() => selectedHandlers[handlerSelectionKey]) === handler.name) {
      sentPick = { key: handlerSelectionKey, name: handler.name };
    }
    return sent;
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
  ): Promise<boolean> {
    const outbound: AgentInboundMessage = { type: handler.name, payload };
    const target = activeTarget;
    if (onSend) return (await onSend(outbound, target?.workflowId ?? null)) !== false;

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
    return true;
  }

  function selectedPickerItem(): PickerItem | null {
    return pickerItems[pickerSelectionIndex] ?? pickerItems[0] ?? null;
  }

  function pickerItemActive(item: PickerItem): boolean {
    return selectedPickerItem()?.id === item.id;
  }

  function isCurrentPickerItem(item: PickerItem): boolean {
    return item.kind === "target"
      ? item.target.workflowId === activeTarget?.workflowId
      : item.handler.name === selectedHandler?.name;
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

  function recallLastMessage(): boolean {
    if (draft !== "" || historyIndex !== -1) return false;
    const latest = sentUserMessages.at(-1);
    if (latest == null) return false;
    historyStash = draft;
    historyIndex = sentUserMessages.length - 1;
    draft = latest;
    return true;
  }

  function restoreRecalledDraft(): boolean {
    if (historyIndex === -1) return false;
    draft = historyStash;
    resetHistoryRecall();
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
      return;
    }

    if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      if (movePickerSelection(-1)) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (event.key === "ArrowUp" && recallLastMessage()) {
        event.preventDefault();
        event.stopPropagation();
        void moveCaretToEnd();
      }
      return;
    }

    if (event.key === "Escape" && restoreRecalledDraft()) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }

    if ((event.key === "Tab" && !event.shiftKey) || event.key === "Enter") {
      if (acceptPickerSelection()) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
    }

    if (scriptInput && event.key === "Tab") {
      event.preventDefault();
      const input = event.currentTarget as HTMLTextAreaElement;
      const start = input.selectionStart;
      const end = input.selectionEnd;
      draft = `${draft.slice(0, start)}  ${draft.slice(end)}`;
      void tick().then(() => input.setSelectionRange(start + 2, start + 2));
      return;
    }

    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      event.stopPropagation();
      void sendMessage();
      return;
    }

    // Any other key (typing, backspace, escape, …) ends history recall so the
    // next ArrowUp re-stashes the edited draft instead of overwriting it.
    resetHistoryRecall();
  }

  async function syncComposerShape(): Promise<void> {
    await tick();
    const input = composerInput;
    if (!input) return;
    composerExpanded =
      scriptInput ||
      draft.includes("\n") ||
      input.scrollHeight > parseFloat(getComputedStyle(input).lineHeight) * 1.6;
  }

  function handleComposerInput(): void {
    dismissedPickerDraft = null;
    void syncComposerShape();
  }

  $effect(() => {
    draft;
    scriptInput;
    void syncComposerShape();
  });

  /* With another text handler to switch to, the control names the current one, so the switch
     is visible without a hover. Form handlers switch on their card. */
  const labelHandler = $derived(textHandlers.length > 1);
  const handlerKind = $derived<HandlerKind>(scriptInput ? "script" : "text");
  const HandlerIcon = $derived(handlerIcon(handlerKind));
  const handlerLabel = $derived(
    selectedHandler
      ? `Handler: ${selectedHandler.name}${targetsSubagent || showTargetPicker ? ` → ${activeTarget?.label ?? agentLabel}` : ""}`
      : ""
  );
  /* What the handler does with a send, said to the reader without a row of its own: the
     button's tip and the field's description. */
  const handlerHint = $derived(
    scriptInput
      ? "Runs this Python as written."
      : textRouteNote ||
          (replyPending && selectedHandler ? midTurnNote(selectedHandler.mid_turn) : "")
  );
  const handlerHintId = $props.id();
  const showRetry = $derived(
    onRetryInterface != null && activeTarget?.interfaceStatus === "failed" && !activeTarget.closed
  );

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    void sendMessage();
  }

</script>

<!-- A turn's header: while it works, what it is doing now, heard once through the chat's
     status line; once settled, what it did. One height and one glyph cell either way, the
     cell holding the caret when the header opens the trace. -->
{#snippet turnHeader(working: boolean, headline: string, summary: TurnActivitySummary | null, opens: boolean)}
  <span class="activity-copy">
    <span class="activity-heading">
      {#if working}
        <span class="sr-only">{replyLabel(pendingPhase)}</span>
        <ReplyStatus
          label={replyLabel(pendingPhase)}
          startedAt={turnRunning ? latestProgress.startedAt : null}
          now={eventNow}
          {live}
        >
          {#if opens}<ChevronRight class="disclosure-chevron" size={13} aria-hidden="true" />{/if}
        </ReplyStatus>
      {:else}
        <span class="trace-glyph">
          {#if opens}<ChevronRight class="disclosure-chevron" size={13} aria-hidden="true" />{/if}
        </span>
        <strong>{headline}</strong>
        {#each summary?.refusals ?? [] as refusal (refusal.word)}
          <span class="turn-refusal" data-tip={`${refusal.count} ${refusal.word}`}>
            <StateGlyph kind={STEP_GLYPHS.rejected} size={12} />{refusal.count}<span class="visually-hidden">{` ${refusal.word} `}</span>
          </span>
        {/each}
      {/if}
      {#if summary}<span class="turn-tokens">{summary.detail}</span>{/if}
    </span>
  </span>
  {#if summary && summary.endedAt > 0}<time>{formatTimestamp(summary.endedAt)}</time>{/if}
{/snippet}

<!-- `embedded headerless` are written out rather than derived: the pane is the
     only place this renders, so both were always what the one call site asked
     for. The rules keyed off them are still the live ones — flattening them into
     the base selectors is a restyle, not a deletion, so the classes stay. -->
<section
  class={`agent-chat embedded headerless ${closed ? "closed" : ""}`}
  aria-label={`${agentLabel} customer chat`}
>
  <div class="chat-shell" style:--queue-reserve={`${queueReserve}px`} style:--queue-cover={`${queueCover}px`} style:--list-gutter={`${listGutter}px`}>
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
    <div class="message-list" tabindex="-1" bind:this={messageListElement} onscroll={trackFollowing} {@attach messageFocus} {@attach followOnResize}>
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
      {:else if runLogs.length === 0 && liveMessages.length === 0 && !replyPending}
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

      <!-- A feed with no articles is announced as an empty feed, so the role waits for rows. -->
      <div
        class="message-rows"
        role={messages.length > 0 ? "feed" : undefined}
        aria-label={messages.length > 0 ? "Messages" : undefined}
        style:height={`${$messageRows.getTotalSize()}px`}
      >
      {#each $messageRows.getVirtualItems() as row (row.key)}
        {@const message = messages[row.index]}
        {#if message}
        {@const copyable = copiesWhole(message)}
        {@const reply = message.role === "assistant"}
        <div
          class="message-row"
          role="article"
          aria-posinset={row.index + 1}
          aria-setsize={messages.length}
          tabindex="-1"
          data-index={row.index}
          style:top={`${row.start}px`}
          {@attach measureMessage}
        >
        <article class={["message", message.role, { latest: row.index === latestReplyIndex }]}>
          <div class={["bubble", { data: message.data, script: message.script, "has-copy": reply && copyable }]}>
            {#if reply && scriptError(message.text)}
              <!-- A script's traceback is code and can run to hundreds of lines: its first line
                   as a trace row, the whole output as a code block behind it. -->
              <TraceRow
                glyph="error"
                status="Script error"
                note={scriptErrorHead(message.text)}
                expandable
                expanded={expandedLogRows.includes(groupKey(message.id, "script-error"))}
                onToggle={() => toggleStep(message.id, "script-error")}
              >
                <LongContent id={`message-${message.id}`} text={message.text} label="Script error" onResize={remeasureMessages}>
                  <MarkdownMessage text={message.text} language="" />
                </LongContent>
              </TraceRow>
            {:else if message.data}
              <JsonReply id={`message-${message.id}`} json={message.json} text={message.text} onResize={remeasureMessages} />
            {:else}
              <LongContent
                id={`message-${message.id}`}
                text={message.text}
                streaming={message.streaming}
                label={reply ? "Agent reply" : "User message"}
                onResize={remeasureMessages}
              >
                <MarkdownMessage
                  text={message.text}
                  citations={message.citations}
                  language={message.script ? "python" : null}
                  streaming={message.streaming}
                />
              </LongContent>
            {/if}
            {#if message.error}
              <LongContent id={`message-${message.id}-error`} text={message.error} label="Reply error" onResize={remeasureMessages}>
                <p class="reply-error">
                  <CircleAlert size={14} aria-hidden="true" />
                  <Copyable value={message.error} label="Copy error"><span>{message.error}</span></Copyable>
                </p>
              </LongContent>
            {/if}
            {#if reply && copyable}
              <span class="message-copy"><Copyable value={message.text} label="Copy message" data-tip-align="end" /></span>
            {/if}
          </div>
          {#if !reply && copyable}
            <span class="message-copy"><Copyable value={message.text} label="Copy message" /></span>
          {/if}
        </article>

        {#if message.role === "user"}
          {@const activityLogs = logsForTurn(message.turnNumber)}
          {@const activeLog = activeLogForTurn(message.turnNumber)}
          {@const working = statusInHeader && turnWorking(message.turnNumber)}
          {@const expanded = activeLog != null && activityExpanded(message.id, message.turnNumber)}
          {#if activeLog || working}
            {@const turnSummary = turnActivitySummary(message.turnNumber, activityLogs)}
            {@const thought = foldTurnThought(activityLogs)}
            {@const headline = working ? "" : turnHeadline(message.turnNumber, turnSummary.duration, thought)}
            <div class={`activity-feed ${expanded ? "expanded" : ""}`}>
              {#if activeLog}
                <button
                  type="button"
                  class={["activity-summary activity-line turn-summary disclosure", working && "active live"]}
                  aria-expanded={expanded}
                  onclick={() => toggleActivity(message.id, message.turnNumber)}
                >
                  {@render turnHeader(working, headline, turnSummary, true)}
                </button>
              {:else}
                <div class="activity-summary activity-line turn-summary active">
                  {@render turnHeader(working, headline, turnSummary, false)}
                </div>
              {/if}

              {#if expanded && activeLog}
                <div class="activity-trace"><div class="activity-list">
                  {#each traceSteps(activityLogs) as step (groupKey(message.id, step.id))}
                    {#if step.kind === "thought"}
                      {#if thought}
                        <details
                          class="activity-row thought"
                          open={openThoughts.includes(groupKey(message.id, thought.turnId))}
                          ontoggle={(event) => setThoughtOpen(groupKey(message.id, thought.turnId), event.currentTarget.open)}
                        >
                          <summary class="thought-summary">
                            <span class="thought-label">{thoughtLabel(thought)}</span>
                            {#if thought.thinking}
                              <StatusChip label="thinking" kind="reasoning" active compact />
                            {/if}
                          </summary>
                          <div class="thought-text">
                            <MarkdownMessage text={thought.text.trim() || NO_THOUGHT_SUMMARY} streaming={thought.thinking} compact />
                          </div>
                        </details>
                      {/if}
                    {:else}
                      {@const outputRow = step.rows.findLast((row) => row.output)}
                      {@const images = toolImages(outputRow?.output, outputRow?.toolName)}
                      <TraceRow
                        glyph={STEP_GLYPHS[step.state]}
                        running={working && (step.state === "running" || step.state === "requested")}
                        prefix={step.nested && step.kind === "tool" ? "host" : undefined}
                        name={step.name}
                        mono={step.kind === "tool" || step.kind === "model"}
                        status={step.status}
                        note={step.note}
                        meta={step.durationMs ? formatElapsedDuration(step.durationMs) : ""}
                        tip={formatTimestamp(step.startedAt)}
                        depth={step.nested ? 1 : 0}
                        expandable={stepHasDetails(step)}
                        expanded={expandedLogRows.includes(groupKey(message.id, step.id))}
                        onToggle={() => toggleStep(message.id, step.id)}
                      >
                        {@const ids = stepIdentifiers(step)}
                        {@const scriptDetail = stepScript(step)}
                        {@const args = stepArguments(step)}
                        {@const fullDetail = stepFullDetail(step)}
                        {#if scriptDetail}
                          <LongContent id={`trace-${step.id}-script`} text={scriptDetail} label="Script details" onResize={remeasureMessages}>
                            <div class="script-detail-wrap"><pre class="activity-script-detail" data-language="python"><code>{scriptDetail}</code></pre></div>
                          </LongContent>
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
                        {#if args}
                          <section class="activity-full" aria-label="Arguments">
                            <span class="activity-full-label">Arguments</span>
                            <JsonReply id={`trace-${step.id}-arguments`} json={args} onResize={remeasureMessages} />
                          </section>
                        {/if}
                        <section class="activity-full" aria-label="Full details">
                          <span class="activity-full-label">Full details</span>
                          <LongContent id={`trace-${step.id}-details`} text={fullDetail} label="Full details" onResize={remeasureMessages}>
                            <Copyable block value={fullDetail} label="Copy full details">
                              <pre class="activity-detail">{fullDetail}</pre>
                            </Copyable>
                          </LongContent>
                        </section>
                        {#snippet media()}
                          {#if images.length}<div class="step-images"><TraceImages {images} /></div>{/if}
                        {/snippet}
                      </TraceRow>
                    {/if}
                  {/each}
                </div></div>
              {/if}
              {#if message.turnNumber === latestScriptUpdateTurn && editorLineCount > 0}
                <TraceRow
                  glyph="complete"
                  name="Script updated"
                  note={`${editorLineCount} lines`}
                  tip={onOpenScript ? "Open the script" : undefined}
                  onActivate={onOpenScript}
                />
              {/if}
            </div>
          {/if}
        {/if}
        </div>
        {/if}
      {/each}
      <!-- A send its turn has not answered yet: the header it will get, in the room the list
           keeps for it, so nothing is measured for it. -->
      {#if statusAtEnd}
        <div class="activity-feed reply-pending" style:height={`${REPLY_SLOT}px`}>
          <div class="activity-summary activity-line turn-summary active">
            {@render turnHeader(true, "", null, false)}
          </div>
        </div>
      {/if}
      </div>
      {#if !following && unseen}
        <Chip class="jump-latest" size="xs" tone="accent" onclick={jumpToLatest}>
          {#snippet lead()}<ArrowDown size={12} />{/snippet}
          Jump to latest
        </Chip>
      {/if}
    </div>

    <p class="sr-only" role="status">{replyAnnounced}</p>

    {#if !closed && error && liveMessages.length > 0}
      <div class="error-banner">
        <Copyable value={error} label="Copy error"><span>{error}</span></Copyable>
      </div>
    {/if}

    {#if closed}
      <div class="closed-banner">
        <CheckCircle2 size={14} aria-hidden="true" />
        <span>Session ended</span>
        {#if onStartNewSession}
          <Chip class="start-new" size="sm" tone="accent" disabled={creatingSession} onclick={onStartNewSession}>
            {#snippet lead()}<Plus size={13} aria-hidden="true" />{/snippet}
            Start a new session
          </Chip>
        {/if}
      </div>
    {/if}

    <!-- Everything the agent is blocked on, in one queue. Always drawn, so an answered last
         request can animate out and the count change is announced. Laid over the end of the
         message list rather than in the grid: the list keeps clear only the folded header,
         and an open card covers the latest messages until it is answered or folded. -->
    <div class="pending-stack" bind:offsetHeight={queueCover}>
      <!-- The action card's title is the composer's named handler control, and its list the
           composer's handler rows, opened in the card rather than over the composer. -->
      {#snippet actionSwitch(locked: boolean)}
        {#if actionHandler}
          <IconButton
            class="choice-button handler-button labeled action-switch"
            label={`Action: ${actionHandler.name}`}
            tip={`Action: ${actionHandler.name}. Choose another to send.`}
            disabled={locked}
            aria-haspopup="listbox"
            aria-expanded={actionMenuOpen}
            aria-controls={actionMenuOpen ? "action-menu" : undefined}
            onclick={toggleActionMenu}
          >
            <ClipboardList size={15} aria-hidden="true" />
            <span class="handler-name fade-end">{actionHandler.name}</span>
            <ChevronDown size={12} aria-hidden="true" />
          </IconButton>
        {/if}
      {/snippet}
      {#snippet actionMenu()}
        {#if actionMenuOpen}
          <div
            class="handler-picker action-menu"
            id="action-menu"
            role="listbox"
            tabindex="-1"
            aria-label="Actions"
            aria-activedescendant={`action-option-${actionMenuIndex}`}
            onkeydown={handleActionMenuKeydown}
            {@attach dismissable({ ondismiss: () => (actionMenuOpen = false), keep: ".action-switch" })}
            {@attach focusOnOpen}
          >
            {#each formHandlers as handler, index (handler.name)}
              {@const current = handler.name === actionHandler?.name}
              <button
                type="button"
                class={["picker-row", { active: index === actionMenuIndex, selected: current }]}
                id={`action-option-${index}`}
                role="option"
                tabindex="-1"
                aria-selected={current}
                title={`${handler.name}. ${midTurnHint(handler.mid_turn)}`}
                onclick={() => switchAction(handler.name)}
                {@attach keepInMenu(index === actionMenuIndex)}
              >
                <span class="picker-name">
                  <strong class="fade-end">{handler.name}</strong>
                  <span class="mid-turn {handler.mid_turn}">{midTurnLabel(handler.mid_turn)}</span>
                  {#if current}<Check size={13} aria-hidden="true" />{/if}
                </span>
                {#if handler.description}<small>{markdownToText(handler.description)}</small>{/if}
              </button>
            {/each}
          </div>
        {/if}
      {/snippet}
      <PendingBannerStack
        rows={queueRows}
        key={(row) => (row === actionRow ? row.id : approvalKey(row))}
        label="Requests waiting on you"
        noun={(row) => (row === actionRow ? "action" : row.event === "callback_requested" ? "response" : "approval")}
        bind:headHeight={queueHead}
        onempty={() => composerInput?.focus()}
      >
        {#snippet banner(request)}
          {#if request === actionRow && actionHandler}
            <!-- Keyed by handler: switching deals that handler's own empty form. -->
            {#key actionHandler.name}
              <HandlerActionCard
                row={request}
                when={actionWhen}
                handler={actionHandler}
                switcher={formHandlers.length > 1 ? actionSwitch : undefined}
                menu={formHandlers.length > 1 ? actionMenu : undefined}
                onsend={(payload) => sendAction(actionHandler, payload)}
                oncancel={textHandlers.length > 0
                  ? () => delete selectedHandlers[handlerSelectionKey]
                  : undefined}
              />
            {/key}
          {:else if request.event === "callback_requested"}
            {@const clientToolStatus = request.toolId ? clientToolStatuses[request.toolId] : undefined}
            {#if clientToolStatus && request.toolId}
              <PendingClientToolCard
                row={request}
                status={clientToolStatus}
                ahead={!viewRowIds.has(request.id)}
                onRetry={onRetryClientTool
                  ? () => onRetryClientTool(request.toolId!)
                  : undefined}
              />
            {:else}
              <PendingCallbackCard
                row={request}
                ahead={!viewRowIds.has(request.id)}
                onSubmit={onCallbackResult && request.toolId
                  ? (outcome) =>
                      onCallbackResult(approvalWorkflowId(request), request.toolId!, outcome)
                  : undefined}
              />
            {/if}
          {:else}
            {@const title = request.toolName || request.body || "Tool approval"}
            {@const busy = !onApproveTool || isApprovalResolving(request)}
            <PendingCard row={request} {title} label={`Approval: ${title}`} ahead={!viewRowIds.has(request.id)}>
              {#snippet action()}
                <!-- Optional and out of the way: Reject alone still rejects in one click. -->
                <IconButton
                  label="Reject with a reason"
                  pressed={rejectReasons[request.id] != null}
                  disabled={busy}
                  onclick={(event) => {
                    const card = (event.currentTarget as HTMLElement).closest(".pending-card");
                    rejectReasons[request.id] = rejectReasons[request.id] == null ? "" : null;
                    void tick().then(() => card?.querySelector<HTMLInputElement>(".reject-reason")?.focus({ preventScroll: true }));
                  }}
                >
                  <MessageSquareText size={14} />
                </IconButton>
              {/snippet}
              {#snippet status()}
                {#if rejectReasons[request.id] != null}
                  <input
                    class="reject-reason"
                    aria-label="Reason for rejecting, sent to the agent (optional)"
                    placeholder="Reason for the agent (optional)"
                    bind:value={rejectReasons[request.id]}
                    onkeydown={(event) => {
                      event.stopPropagation();
                      if (event.key === "Enter") void resolveApproval(event, request, false);
                    }}
                  />
                {/if}
                {#if approvalDecidedElsewhere(request)}
                  <p class="approval-note">Already decided</p>
                {:else if approvalError(request)}
                  <p class="approval-error" role="alert">{approvalError(request)}</p>
                {/if}
              {/snippet}
              {#snippet foot()}
                <Chip fill="quiet" dense label="Approve" aria-label="Approve" data-tip="Approve" disabled={busy} onclick={(event) => void resolveApproval(event, request, true)}>
                  {#snippet lead()}<Check size={12} />{/snippet}
                </Chip>
                <Chip fill="quiet" dense label="Always allow" aria-label="Always allow" data-tip="Always allow" disabled={busy} onclick={(event) => void resolveApproval(event, request, true, true)}>
                  {#snippet lead()}<ShieldCheck size={12} />{/snippet}
                </Chip>
                <Chip tone="error" fill="quiet" toned dense label="Reject" aria-label="Reject" data-tip="Reject" disabled={busy} onclick={(event) => void resolveApproval(event, request, false)}>
                  {#snippet lead()}<CircleX size={12} />{/snippet}
                </Chip>
              {/snippet}
            </PendingCard>
          {/if}
        {/snippet}
      </PendingBannerStack>
    </div>

    <div class="composer-wrap">
      {#if queuedMessages.length > 0}
        <p
          class="composer-hint-slot"
          aria-label="Queued messages"
          data-tip={queuedMessageTip}
        >
          <span>{queuedMessages.length} waiting</span>
          {#if queuedMessages.length === 1}
            <span aria-hidden="true">·</span>
            <span class="queued-preview">{queuedMessagePreview}</span>
          {/if}
        </p>
      {/if}
      {#if pickerOpen}
        <div
          class={["handler-picker", { "chip-open": pickerButtonOpen }]}
          id="handler-menu"
          role="listbox"
          tabindex="-1"
          aria-label="Accepted messages"
          aria-activedescendant={pickerButtonOpen ? `picker-option-${pickerSelectionIndex}` : undefined}
          onkeydown={(event: KeyboardEvent) => pickerButtonOpen && handlePickerKeydown(event)}
          {@attach dismissable({ ondismiss: closePicker, keep: ".handler-button" })}
          {@attach pickerButtonOpen && focusOnOpen}
        >
          <p class="picker-hint">
            {handlers.length === 0
              ? "This agent declares no accepted messages."
              : `Send to ${activeTarget?.label ?? agentLabel}`}
          </p>
          {#each pickerItems as item, index (item.id)}
            {#if item.kind === "target"}
              <button
                type="button"
                class={["picker-row target", { active: pickerItemActive(item), selected: isCurrentPickerItem(item) }]}
                id={`picker-option-${index}`}
                role="option"
                tabindex="-1"
                aria-selected={isCurrentPickerItem(item)}
                title={item.target.label}
                onclick={() => selectTarget(item.target)}
                {@attach keepInMenu(pickerItemActive(item))}
              >
                <strong class="fade-end">{item.target.label}</strong>
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
                class={["picker-row", { active: pickerItemActive(item), selected: isCurrentPickerItem(item) }]}
                id={`picker-option-${index}`}
                role="option"
                tabindex="-1"
                aria-selected={isCurrentPickerItem(item)}
                title={`${item.handler.name}. ${midTurnHint(item.handler.mid_turn)}`}
                onclick={() => selectHandler(item.handler)}
                {@attach keepInMenu(pickerItemActive(item))}
              >
                <span class="picker-name">
                  <strong class="fade-end">{item.handler.name}</strong>
                  <span class="mid-turn {item.handler.mid_turn}">
                    {midTurnLabel(item.handler.mid_turn)}
                  </span>
                  {#if isCurrentPickerItem(item)}<Check size={13} aria-hidden="true" />{/if}
                </span>
                <small>{markdownToText(item.handler.description)}</small>
              </button>
            {/if}
          {/each}
        </div>
      {:else if openChoice}
        {@const choice = openChoice}
        <!-- Keyed: opening another handler's list mounts a new layer, so it takes focus and
             hands it back to its own button. -->
        {#key choice.handler.name}
        <div
          class="handler-picker choice-menu"
          id="choice-menu"
          role="listbox"
          tabindex="-1"
          aria-label={choice.label}
          aria-activedescendant={choiceIndex < 0 ? undefined : `choice-option-${choiceIndex}`}
          onkeydown={(event: KeyboardEvent) => handleChoiceKeydown(event, choice)}
          {@attach dismissable({ ondismiss: () => (openChoiceName = null), keep: ".choice-button" })}
          {@attach focusOnOpen}
        >
          <p class="picker-hint">{choice.label} for {activeTarget?.label ?? agentLabel}</p>
          {#each choice.field.choices ?? [] as option, index (option)}
            <button
              type="button"
              class={["picker-row", { active: index === choiceIndex, selected: option === choice.value }]}
              id={`choice-option-${index}`}
              role="option"
              tabindex="-1"
              aria-selected={option === choice.value}
              title={option}
              onclick={() => void pickChoice(choice, option)}
              {@attach keepInMenu(index === choiceIndex)}
            >
              <span class="picker-name">
                <strong class="fade-end">{choiceText(option)}</strong>
                {#if option === choice.value}<Check size={13} aria-hidden="true" />{/if}
              </span>
            </button>
          {/each}
        </div>
        {/key}
      {/if}

      {#if handlerHint}<span id={handlerHintId} class="visually-hidden">{handlerHint}</span>{/if}

      {#snippet handlerSwitch()}
        {#if selectedHandler}
          <IconButton
            class={`choice-button handler-button${offDefaultHandler ? " revealed" : ""}${labelHandler ? " labeled" : ""}`}
            label={handlerLabel}
            tip={[
              `${handlerLabel}.`,
              scriptInput ? handlerHint : "",
              replyPending ? midTurnHint(selectedHandler.mid_turn) : "",
              "Type / to choose."
            ].filter(Boolean).join(" ")}
            disabled={composerDisabled}
            aria-describedby={handlerHint ? handlerHintId : undefined}
            aria-haspopup="listbox"
            aria-expanded={pickerOpen}
            aria-controls={pickerOpen ? "handler-menu" : undefined}
            onclick={togglePicker}
          >
            <HandlerIcon size={15} aria-hidden="true" />
            {#if labelHandler}
              <span class="handler-name fade-end">{selectedHandler.name}</span>
              <ChevronDown size={12} aria-hidden="true" />
            {/if}
          </IconButton>
        {/if}
        {#each choiceHandlers as choice (choice.handler.name)}
          {@const open = openChoiceName === choice.handler.name}
          <!-- aria-disabled, not disabled, so a send in flight keeps focus on the button and
               its tip readable; toggleChoice and pickChoice refuse while it is set. -->
          <IconButton
            class="choice-button"
            label={choice.tip}
            aria-disabled={choiceBlocked(choice) && !open ? "true" : undefined}
            aria-haspopup="listbox"
            aria-expanded={open}
            aria-controls={open ? "choice-menu" : undefined}
            onclick={() => toggleChoice(choice)}
          >
            <choice.icon size={15} aria-hidden="true" />
          </IconButton>
        {/each}
      {/snippet}

        <form
          class={[
            "composer",
            {
              closed,
              expanded: composerExpanded || scriptInput || attachments.length > 0,
              "code-input": scriptInput
            }
          ]}
          onsubmit={handleSubmit}
          ondragover={(event) => {
            if (attachmentInput && event.dataTransfer?.types.includes("Files")) event.preventDefault();
          }}
          ondrop={handleComposerDrop}
          onpaste={handleComposerPaste}
        >
          {#if attachmentInput && attachments.length > 0}
            <ul class="composer-attachments" aria-label="Attachments">
              {#each attachments as attachment (attachment.id)}
                <li>
                  <Chip
                    size="xs"
                    tone="neutral"
                    label={attachment.name}
                    aria-label={`Remove attachment ${attachment.name}`}
                    title={`Remove ${attachment.name}`}
                    onclick={() => removeAttachment(attachment.id)}
                  >
                    {#snippet lead()}<CircleX size={12} />{/snippet}
                  </Chip>
                </li>
              {/each}
            </ul>
          {/if}
          <textarea
            use:inputClearDissolve
            bind:this={composerInput}
            bind:value={draft}
            rows="1"
            placeholder={composerPlaceholder}
            aria-label={`Message ${activeTarget?.label ?? agentLabel}`}
            aria-describedby={handlerHint ? handlerHintId : undefined}
            disabled={composerDisabled || !canSendToTarget}
            spellcheck={!scriptInput}
            oninput={handleComposerInput}
            onkeydown={handleComposerKeydown}
          ></textarea>
          <div class="composer-controls">
            {#if attachmentInput}
              <input
                class="attachment-input"
                bind:this={attachmentInputElement}
                type="file"
                accept={attachmentInput.accept || undefined}
                multiple={attachmentInput.multiple}
                aria-label="Choose attachments"
                onchange={(event) => {
                  if (event.currentTarget.files) void addFiles(event.currentTarget.files);
                  event.currentTarget.value = "";
                }}
              />
              <IconButton
                label="Add attachments"
                tip="Add photos or files"
                tone="quiet"
                onclick={() => attachmentInputElement?.click()}
              >
                <Plus size={16} />
              </IconButton>
            {/if}
            {@render handlerSwitch()}
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
            <span class="composer-control-spacer"></span>
            {#if contextUsage && !targetsSubagent}<ContextUsage usage={contextUsage} />{/if}
            <IconButton
              type="submit"
              label="Send message"
              tip={textRouteNote || "Send message"}
              tone="accent"
              disabled={!canSendDraft}
              aria-describedby={textRouteNote ? handlerHintId : undefined}
            >
              <ArrowUp size={17} />
            </IconButton>
          </div>
          {#if attachmentError}
            <p class="attachment-error" role="alert">{attachmentError}</p>
          {/if}
          {#if textRouteNote && draft.trim()}
            <p class="composer-note" aria-hidden="true">{textRouteNote}</p>
          {/if}
        </form>

    </div>
  </div>

</section>

<style>
  .agent-chat {
    container: agent-chat / inline-size;
    /* The messages' side inset, which the attention queue over them shares. */
    --chat-inset: clamp(18px, 5vw, 72px);
    width: 100%;
    height: 100%;
    min-height: 0;
    display: grid;
    gap: 0;
    background: var(--surface-0);
  }

  .agent-chat.embedded {
    --chat-inset: var(--pane-inset);
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

  /* Named rows, so an absent banner never moves the composer into a row sized for something
     else. The attention queue takes no row: it lies over the end of the messages, so however
     many requests wait, the transcript and the composer keep their room. */
  .agent-chat.headerless .chat-shell {
    grid-template-rows: minmax(0, 1fr) auto auto;
    grid-template-areas: "messages" "banner" "composer";
  }

  .message-list {
    grid-area: messages;
    min-height: 0;
    overflow-y: auto;
    /* Never sideways: wide content scrolls inside its own block. Without this, anything
       painted past a message's edge — even an invisible, absolutely placed tooltip on a
       control at the bubble's corner — becomes scroll overflow and a pane-wide scrollbar. */
    overflow-x: hidden;
    overflow-anchor: none;
    --message-gap: 16px;
    display: flex;
    flex-direction: column;
    gap: var(--message-gap);
    scrollbar-gutter: stable;
    padding: 22px var(--chat-inset);
    padding-inline-end: calc(var(--chat-inset) - var(--scrollbar-size));
    padding-bottom: calc(22px + var(--queue-reserve, 0px));
    /* Whatever a message stacks — a citation's tooltip — stays under the replay strip. */
    isolation: isolate;
  }

  .agent-chat.embedded .message-list {
    --message-gap: 12px;
    padding-top: 14px;
    padding-bottom: calc(14px + var(--queue-reserve, 0px));
  }

  .empty-chat {
    align-self: center;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    margin-top: 12vh;
    color: var(--text-2);
    font-size: var(--font-lg);
  }

  .empty-chat.error {
    color: var(--error);
  }

  .empty-chat.closed-empty {
    color: var(--success);
  }

  .message-rows {
    position: relative;
    flex: 0 0 auto;
  }

  /* Placed by the virtualizer; the gap is padding so it is part of the measured height. */
  .message-row {
    position: absolute;
    inset-inline: 0;
    display: flex;
    flex-direction: column;
    gap: var(--message-gap);
    padding-bottom: var(--message-gap);
  }

  /* With no frames, rhythm separates turns: a user's message opens a turn a step further from
     the one before than a turn's own parts sit from each other. */
  .message-row:not([data-index="0"]):has(> .message.user) {
    padding-top: var(--gap-md);
  }

  /* Rides the visible foot of the scroller, above the attention queue lying over it. */
  .message-list :global(.jump-latest) {
    position: sticky;
    bottom: var(--queue-cover, 0px);
    z-index: 1;
    flex: none;
    align-self: center;
  }

  .message-row:focus-visible {
    outline-offset: -2px;
  }

  .message {
    min-width: 0;
    display: flex;
    gap: 10px;
  }

  .message.user {
    justify-content: flex-end;
  }

  /* A reply starts at the pane's edge, the turn header's, with nothing beside it. */
  .message.assistant {
    flex-direction: column;
    align-items: flex-start;
  }

  /* At the bubble's foot, on the side facing the middle of the pane. */
  .message-copy {
    flex: none;
    align-self: flex-end;
  }

  .message.user .message-copy {
    order: -1;
  }

  /* The whole message reveals it, not only the control's own box; the latest reply keeps
     it shown, as the one most often copied. */
  .message:hover .message-copy :global(.copy),
  .message:focus-within .message-copy :global(.copy),
  .message.latest .message-copy :global(.copy) {
    opacity: 1;
  }

  /* A hairline box means pressable, so only the user's own message keeps one: it marks whose
     turn it is. A reply is prose on the pane, at the column's full width. */
  .bubble {
    min-width: 0;
    color: var(--text-1);
    line-height: 1.5;
  }

  .message.assistant .bubble {
    align-self: stretch;
    --markdown-font-family: var(--font-sans);
    --markdown-body-size: 14px;
    --markdown-body-line-height: 1.5;
    --markdown-heading-size: 14.5px;
    --markdown-heading-line-height: 1.42;
    --markdown-block-gap: var(--gap-md);
    --markdown-list-gap: var(--gap-xs);
    --markdown-strong-weight: 680;
  }

  /* Replies run long, so their rhythm is tight: one block gap throughout, a little more above
     a heading, close-set lists, and a rule no taller than a gap. The measure stops at 72ch on a
     wide pane; a narrow one is already shorter. */
  .message.assistant .bubble :global(.markdown-message) {
    max-width: 72ch;
  }

  .message.assistant .bubble :global(.markdown-message > :not(:first-child) > :is(h3, h4, h5, h6)),
  .message.assistant .bubble :global(.markdown-message > [data-lead]:not(:first-child)) {
    margin-top: var(--gap-sm);
  }

  .message.assistant .bubble :global(li > :is(ul, ol)) {
    gap: calc(var(--gap-xs) / 2);
  }

  .message.assistant .bubble :global(hr) {
    height: 1px;
    margin: 0;
    border: 0;
    background: var(--border);
  }

  .message.user .bubble {
    max-width: min(620px, 72%);
    padding: 12px 14px;
    border: 1px solid color-mix(in srgb, var(--accent) 32%, transparent);
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
  }

  .message.user .bubble :global(.markdown-message p) {
    white-space: pre-wrap;
  }

  .agent-chat.embedded .message.user .bubble {
    max-width: min(100%, 560px);
  }

  /* A script is code: its code block is the frame, and a long line scrolls rather than wraps,
     so indentation reads as written. */
  .message.user .bubble.script {
    padding: 0;
    border: 0;
    background: none;
  }

  .bubble.script :global(pre code) {
    white-space: pre;
    overflow-wrap: normal;
  }

  /* A reply's copy sits at its foot, at the end, in room kept for it, so revealing it never
     re-measures the row. A code block's copy holds the top corner. */
  .bubble.has-copy {
    position: relative;
    padding-bottom: var(--control-height-xs);
  }

  /* Flex, so the copy's absolutely placed live region starts inside it rather than one pixel
     past the pane's end, where it became sideways scroll. */
  .bubble.has-copy > .message-copy {
    position: absolute;
    bottom: 0;
    inset-inline-end: 0;
    display: flex;
  }

  /* The composer choice buttons' quiet icon: no chrome at rest, the hairline only on its own
     hover or keyboard focus. */
  .message-copy :global(.icon-button.copy) {
    border-color: transparent;
    background: transparent;
    color: var(--text-3);
  }

  .message-copy :global(.icon-button.copy:hover),
  .message-copy :global(.icon-button.copy:focus-visible) {
    border-color: var(--border);
    background: transparent;
    color: var(--text-2);
  }

  /* Said by its icon and the error tone, under the reply it ended. */
  .reply-error {
    margin: var(--gap-md) 0 0;
    display: flex;
    gap: var(--gap-sm);
    align-items: flex-start;
    overflow-wrap: anywhere;
    color: var(--error);
    font-size: var(--font-md);
  }

  .reply-error > :global(svg) {
    flex: none;
    margin-top: 3px;
  }

  /* No frame: the turn's header is the pressable part, and the trace hangs under it. */
  .activity-feed {
    position: relative;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }

  .activity-feed.expanded {
    gap: 8px;
  }

  /* Opens from nothing to its own height; the windowed list measures the row as it grows. */
  .activity-trace {
    display: grid;
    grid-template-rows: 1fr;
    transition: grid-template-rows var(--duration-pane) var(--ease-pane);
  }

  @starting-style {
    .activity-trace {
      grid-template-rows: 0fr;
    }
  }

  /* Clipped only so it can open from nothing; the padding is the room a highlighted row's
     bleed needs, given back by the margin on the start side, like the header's. */
  .activity-list {
    min-height: 0;
    overflow: hidden;
    display: grid;
    gap: 6px;
    margin-block: -4px;
    margin-inline-start: -6px;
    padding: 4px 6px;
    /* The rule the steps hang on, through the middle of their icons. */
    background: linear-gradient(var(--border), var(--border)) no-repeat 14px 0 / 1px 100%;
  }

  .activity-list:dir(rtl) {
    background-position: calc(100% - 14px) 0;
  }

  /* The glyph column sits on the rule, so each glyph masks the stretch it covers. */
  .activity-list :global(.trace-glyph) {
    background: var(--surface-0);
  }

  /* Three weights, by the step's glyph: a finished step recedes, one in flight keeps the
     trace's own weight beside its spinner, and one that failed or waits on someone (signalStep)
     reads at full contrast, its error line too. Each glyph's shape differs as well. */
  .activity-list :global(.trace-row.glyph-complete > .trace-line strong) {
    color: var(--text-3);
  }

  .activity-list :global(.trace-row:is(.glyph-error, .glyph-approval) > .trace-line :is(strong, .trace-note)) {
    color: var(--text-1);
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
  }

  /* Pressable like a Files tree row: no resting frame, the caret, and a hover tint. The tint
     bleeds out on the start side only, so the caret lines up with the reply and nothing
     overflows toward the scrollable end. */
  .activity-summary {
    margin-inline-start: calc(-1 * var(--gap-sm));
    padding: 0 var(--gap-sm);
    border: 0;
    border-radius: var(--radius-sm);
    background: transparent;
    font: inherit;
    text-align: start;
  }

  .activity-summary {
    cursor: pointer;
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

  @media (hover: hover) and (pointer: fine) {
    .activity-summary.disclosure:hover {
      color: var(--text-2);
      background: var(--control-hover);
    }
  }

  .activity-summary:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
    outline-offset: 2px;
  }

  /* A finished turn's header recedes to the quiet tone so the reply under it leads. It keeps
     the trace's weight while the turn works, while it is open, and when focused or hovered. */
  .activity-line.active,
  .activity-summary[aria-expanded="true"],
  .activity-summary:focus-visible {
    color: var(--text-2);
  }

  /* The caret holds its cell at rest, quiet, as a tree row's glyph does, so the room before
     the label is a mark rather than a hole. A working header's loader keeps the cell. */
  .turn-summary.disclosure:not(.live) :global(.disclosure-chevron) {
    opacity: 1;
  }

  .activity-copy {
    min-width: 0;
    overflow: hidden;
  }

  /* The live status's height whether working or settled, so a turn ending never re-measures
     its row. The timestamp takes an implicit column, so a narrow pane that hides it leaves
     no gap after the token total. */
  .activity-summary {
    height: var(--control-height-xs);
    grid-template-columns: minmax(0, 1fr);
    grid-auto-flow: column;
  }

  .activity-summary .activity-copy {
    display: grid;
    gap: 3px;
  }

  .activity-heading {
    min-width: 0;
    display: inline-flex;
    gap: var(--gap-sm);
    align-items: center;
    overflow: hidden;
    white-space: nowrap;
  }

  .activity-copy strong {
    flex: 0 0 auto;
    font-weight: 650;
  }

  /* Narrow, the label gives way and the figures beside it never do. */
  .activity-heading > strong {
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* The trace's reject glyph and a count; the word is the tip's and the screen reader's. */
  .turn-refusal {
    --status-color: var(--error);
    flex: none;
    display: inline-flex;
    gap: 3px;
    align-items: center;
    font-variant-numeric: tabular-nums;
  }

  /* At the row's end, beside the timestamp, so a settled header has no hole on its right. */
  .turn-tokens {
    flex: none;
    margin-inline-start: auto;
    font-variant-numeric: tabular-nums;
  }

  @container agent-chat (max-width: 480px) {
    .turn-summary time {
      display: none;
    }
  }

  .activity-line time {
    color: var(--text-3);
    font-size: var(--font-sm);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  /* Under the row's text, at the indent its details use. */
  .step-images {
    margin-left: 30px;
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

  .activity-list .row-ids {
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

  /* Out of flow, over the foot of the message list's area and in its column, edge to edge
     with the messages: above them, under the replay strip and the composer's picker. Never
     taller than the area, so on a short pane the card scrolls between its sticky count and
     its sticky actions. The queue draws its own opaque surface, so it can come and go. The
     grid area includes the list's scrollbar, which the messages' end inset already counts as
     --scrollbar-size; the queue swaps that for the measured track, so it ends where the
     messages do and never runs onto a track wider than the token (Firefox's). */
  .pending-stack {
    position: absolute;
    grid-area: messages;
    inset: auto var(--chat-inset) 0;
    inset-inline-end: calc(var(--chat-inset) - var(--scrollbar-size) + var(--list-gutter, 0px));
    z-index: 1;
    max-height: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .reject-reason {
    min-width: 0;
    box-sizing: border-box;
    padding: 6px 9px;
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-1);
    font: inherit;
    font-size: var(--font-md);
  }

  .activity-feed.reply-pending {
    position: absolute;
    inset-inline: 0;
    bottom: 0;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
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
    margin: 0 var(--chat-inset) 10px;
  }

  .closed-banner {
    grid-area: banner;
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
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

  .closed-banner > :global(svg) {
    flex: 0 0 auto;
    color: var(--success);
  }

  .closed-banner :global(.start-new) {
    flex: none;
    margin-inline-start: auto;
  }

  .agent-chat.embedded .closed-banner {
    margin: 0 var(--chat-inset) 10px;
  }

  .composer-wrap {
    grid-area: composer;
    position: relative;
    margin: 0 clamp(18px, 5vw, 72px) 18px;
    padding-top: var(--pane-inset);
    border-top: 1px solid var(--border);
  }

  .agent-chat.embedded .composer-wrap {
    margin: 0 var(--chat-inset) 12px;
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

  /* Only the vertical offset tracks the chip (so a tall, expanded composer still opens the
     picker right above its row); left/right stay the base rule's 0/0. Anchoring the picker's
     own width to the chip's right edge let it overhang past the composer's left edge, since
     the chip sits mid-row with the send button and spacer still to its right. */
  @supports (anchor-name: --handler-picker) {
    .handler-picker.chip-open {
      position-anchor: --handler-picker;
      bottom: calc(anchor(top) + 8px);
    }
  }

  /* The same list opened inside the action card, in its flow, so the stack grows to hold it. */
  .handler-picker.action-menu {
    position: static;
    max-height: 240px;
    box-shadow: none;
  }

  /* The composer's named handler control as the action card's title: no box until hovered,
     focused or open, the name at the card titles' weight, the icon and chevron quiet. The
     border stays 1px at rest, so the reveal moves nothing. */
  .pending-stack :global(.icon-button.action-switch) {
    flex: 0 1 auto;
    width: auto;
    min-width: 0;
    max-width: 100%;
    gap: var(--gap-xs);
    margin-inline-start: calc(-1 * var(--gap-sm) - 1px);
    padding-inline: var(--gap-sm);
    border-color: transparent;
    background: transparent;
    color: var(--text-1);
    font-family: var(--font-mono);
    font-size: var(--font-md);
    font-weight: 600;
  }

  .pending-stack :global(.icon-button.action-switch:hover:not(:disabled)),
  .pending-stack :global(.icon-button.action-switch:focus-visible),
  .pending-stack :global(.icon-button.action-switch[aria-expanded="true"]) {
    border-color: var(--border);
    background: transparent;
    color: var(--text-1);
  }

  .pending-stack :global(.icon-button.action-switch svg) {
    flex: none;
    color: var(--text-3);
  }

  .composer-hint-slot {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: var(--gap-xs);
    margin-bottom: var(--gap-sm);
    overflow: hidden;
    color: var(--text-3);
    font-size: var(--font-sm);
    white-space: nowrap;
  }

  .queued-preview {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
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

  /* The list holds focus so its arrow keys work; the active row is where the keyboard is. */
  .handler-picker:focus {
    outline: none;
  }

  /* The current value: a tint, so the keyboard's place (.active) can still be a box of its own. */
  .picker-row.selected {
    background: var(--selected-bg);
  }

  .picker-row.target {
    border-color: color-mix(in srgb, var(--model) 36%, var(--border));
    background: color-mix(in srgb, var(--model) 13%, var(--surface-2));
  }

  .picker-row strong {
    min-width: 0;
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

  .picker-name > :global(svg) {
    flex: none;
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

  .composer {
    --composer-max-height: 11.6em;
    display: grid;
    /* The prompt keeps room to type in; the controls shrink and wrap before it does. */
    grid-template-columns: minmax(min(6em, 30%), 1fr) auto;
    gap: var(--gutter-tight);
    align-items: center;
    padding: var(--gutter-tight);
    border: 1px solid var(--border);
    border-radius: 0;
    background: var(--control-bg);
    color: var(--text-3);
    font-family: var(--font-sans);
    font-size: var(--font-lg);
    line-height: 1.45;
  }

  .composer.expanded {
    grid-template-columns: minmax(0, 1fr);
    align-items: end;
  }

  .composer.expanded textarea {
    grid-row: 1;
  }

  .composer-attachments {
    grid-column: 1 / -1;
    grid-row: 1;
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .composer.expanded:has(.composer-attachments) textarea {
    grid-row: 2;
  }

  .composer.expanded:has(.composer-attachments) .composer-controls {
    grid-row: 3;
  }

  .composer-attachments li {
    min-width: 0;
    max-width: min(240px, 70vw);
  }

  .composer-attachments :global(.chip) {
    max-width: 100%;
  }

  .attachment-input {
    display: none;
  }

  .composer-controls {
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
  }

  .composer.expanded .composer-controls {
    grid-row: 2;
  }

  .attachment-error,
  .composer-note {
    grid-column: 1 / -1;
    margin: 0;
    color: var(--error);
    font-size: var(--font-sm);
  }

  .composer-note {
    color: var(--text-3);
  }

  .composer-controls :global(.handler-button) {
    anchor-name: --handler-picker;
  }

  .composer-controls :global(.choice-button) {
    flex: none;
  }

  /* Named, the handler control is the one choice that may shrink: a long name fades at its
     end instead of pushing the send button onto a row of its own. */
  .composer-controls :global(.icon-button.handler-button.labeled) {
    flex: 0 1 auto;
    width: auto;
    min-width: 0;
    max-width: 100%;
    gap: var(--gap-xs);
    padding-inline: var(--gap-sm);
    font-size: var(--font-md);
  }

  .composer-controls :global(.handler-button.labeled svg) {
    flex: none;
  }

  .handler-name {
    min-width: 0;
  }

  /* Default IconButton chrome is a filled box. These used to be quiet chips,
     so at rest the border width stays reserved and only its color drops, and
     the icon uses that chip's --text-3 (about 5.7:1 on --control-bg). Hovering
     or focusing the field, including while typing, leaves them that way. The
     button's own hover, keyboard focus, or an open list brings the hairline
     and --text-2 back. The hover selector has to name .icon-button too:
     IconButton's own hover is specific enough to paint its filled box back on. */
  .composer-controls :global(.choice-button) {
    border-color: transparent;
    background: transparent;
    color: var(--text-3);
  }

  .composer-controls :global(.icon-button.choice-button:hover:not(:disabled, [aria-disabled="true"])),
  .composer-controls :global(.icon-button.choice-button:focus-visible),
  .composer-controls :global(.icon-button.choice-button[aria-expanded="true"]),
  .composer-controls :global(.icon-button.handler-button.revealed) {
    border-color: var(--border);
    background: transparent;
    color: var(--text-2);
  }

  /* An auto margin, not a growing spacer: it takes only the room the chips have not grown into.
     The negative end margin gives back the row gap this empty item would otherwise add, which
     is the last few pixels a full row of choice icons needs at the narrowest pane. */
  .composer-control-spacer {
    flex: none;
    margin-inline-start: auto;
    margin-inline-end: calc(-1 * var(--gap-sm));
  }

  /* Match the shared field focus treatment; the textarea leaves its own box to this surface. */
  .composer:focus-within {
    outline: 2px solid var(--focus-ring);
    outline-offset: -1px;
  }

  /* The same token the send button beside it is built from, so the pair matches:
     IconButton is --control-height, and this was left on a literal 32px when the
     button adopted it. The drift checker cannot catch this one — it only looks at
     heights near a `cursor: pointer`, and a text field has none. */
  .composer textarea {
    min-width: 0;
    min-height: var(--control-height);
    max-height: min(var(--composer-max-height), 35vh);
    overflow-y: auto;
    field-sizing: content;
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    resize: none;
    appearance: none;
    border: 0;
    border-radius: 0;
    outline: none;
    background: none;
    box-shadow: none;
    color: var(--text-1);
    caret-color: var(--accent);
    font: inherit;
  }

  .composer:not(.expanded) textarea {
    height: var(--control-height);
    padding-block: calc((var(--control-height) - 1lh) / 2);
  }

  .composer.code-input {
    --composer-max-height: 23.2em;
    font-family: var(--font-mono);
    font-size: var(--font-md);
  }

  .composer.code-input textarea {
    tab-size: 2;
  }

  .composer textarea::placeholder {
    color: var(--text-3);
  }

  /* A wrapped placeholder overflows the one-line box and shows the textarea's scrollbar track;
     kept to one line, it is clipped rather than scrolled. */
  .composer:not(.expanded) textarea {
    overflow-x: hidden;
  }

  .composer:not(.expanded) textarea::placeholder {
    white-space: nowrap;
  }

  .composer textarea:disabled {
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

    .agent-chat {
      --chat-inset: 16px;
    }

    .message.user .bubble {
      max-width: 88%;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    /* A trace still opens; it just stops moving there. The choice button's
       border and icon fade on the same tokens, and that fades out too. */
    .activity-trace,
    .composer-controls :global(.choice-button) {
      transition: none;
    }
  }
</style>
