import type {
  AgentInboundMessage,
  AgentInterfaceFunction,
  AgentMessageObject,
  AgentSseFrame,
  OperatorCommand,
  OperatorCommandResponse,
  WorkflowExecutionState
} from "$lib/api/types";
import type { AgentApi } from "$lib/api/client";
import type { AgentDescriptor, Session } from "$lib/api/types";
import { SYNTHESIZED } from "$lib/api/types";
import { HttpAgentApi } from "$lib/api/httpClient";
import { realisticQaScenario } from "$lib/mock/scenarios";
import { buildUsageTimeline, summarizeCost } from "$lib/cost/pricing";
import { chooseBootSession } from "./bootSession";
import {
  buildAgentTreeGraph,
  type AgentGraphSource
} from "./flowProjection";
import {
  catchUpCeilingMs,
  catchingUpAfterFrame,
  cursorAfterPublish,
  framePublishChunkSize,
  publishAtChunkBoundary
} from "./hydration";
import { buildReplayLog, buildReplayMarkers } from "./replayLog";
import { buildStepTimeline, type StepTimelineFrame } from "./stepTimeline";
import { buildTranscript } from "./transcript";

export type PlaybackSpeed = 1 | 2 | 5 | 10;

export interface RunInfo {
  sessionId: string;
  agentLabel: string;
  models: string[];
  startedAt: number;
}

export interface ObservedSubagent {
  workflowId: string;
  role: "subagent";
  parentWorkflowId: string;
  subagentId: string;
  agentKey: string;
  label: string;
  agentInterface?: AgentInterfaceFunction[];
  operatorInterface?: OperatorCommand[];
  targetTurn: number | null;
  stopped: boolean;
}

export interface OperatorTarget {
  workflowId: string;
  role: "parent" | "subagent";
  label: string;
  operatorInterface: OperatorCommand[];
  closed: boolean;
}

type ReplayTimelineRole = "parent" | "subagent";

interface ReplayTimelineEntry extends StepTimelineFrame {
  workflowId: string;
  role: ReplayTimelineRole;
  frame: AgentSseFrame;
}

const basePlaybackDelayMs = 700;
const activeSessionStorageKey = "temporal-agent-ui.active-session.v1";
const frameCacheStorageKeyPrefix = "temporal-agent-ui.frames.v1:";

function frameCacheStorageKey(sessionId: string): string {
  return `${frameCacheStorageKeyPrefix}${sessionId}`;
}

function readStoredActiveSessionId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const value = window.localStorage.getItem(activeSessionStorageKey);
    return value && value.trim() ? value : null;
  } catch {
    return null;
  }
}

function writeStoredActiveSessionId(sessionId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(activeSessionStorageKey, sessionId);
  } catch {
    // Ignore storage failures; active session persistence is a UI convenience.
  }
}

function removeStoredActiveSessionId(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(activeSessionStorageKey);
  } catch {
    // Ignore storage failures.
  }
}

function now(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

/**
 * Backoff before re-opening a stream that dropped mid-run, and the cap on how
 * many times to try.
 *
 * Capped because an unbounded loop against a server that is genuinely down is
 * worse than going quiet: it never stops, never surfaces the error, and buries
 * the real one under retries.
 *
 * The tail repeats at the cap rather than doubling further, and the total —
 * about half a minute — is sized for the one outage nothing else here can
 * recover from: a server restart. Losing the network fires an `online` event
 * on the way back (see #reattachWhenOnline), and a sleeping machine suspends
 * these timers so the budget survives the nap. A server that is down while the
 * network stays up produces neither signal, so the wait for it has to be spent
 * here.
 */
const reattachBackoffMs = [500, 1_000, 2_000, 4_000, 8_000, 8_000, 8_000];

/** Sleep, unless the stream is abandoned first. */
function sleepUnlessAborted(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    let timer: ReturnType<typeof setTimeout>;
    const settle = (): void => {
      clearTimeout(timer);
      signal.removeEventListener("abort", settle);
      resolve();
    };
    timer = setTimeout(settle, ms);
    signal.addEventListener("abort", settle, { once: true });
  });
}

/**
 * Hand the main thread back so the browser can paint and answer input.
 *
 * A rAF rather than a bare timeout, because the point is to let a paint happen:
 * resuming before one has means the work was interleaved without the page ever
 * catching up.
 */
function yieldToMain(): Promise<void> {
  return new Promise((resolve) => {
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(() => resolve());
      return;
    }
    setTimeout(resolve, 0);
  });
}

function readCachedFrames(sessionId: string): AgentSseFrame[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.sessionStorage.getItem(frameCacheStorageKey(sessionId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as { frames?: unknown };
    return Array.isArray(parsed.frames) ? (parsed.frames as AgentSseFrame[]) : [];
  } catch {
    return [];
  }
}

function writeCachedFrames(sessionId: string, frames: AgentSseFrame[]): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(
      frameCacheStorageKey(sessionId),
      JSON.stringify({ frames, savedAt: Date.now() })
    );
  } catch {
    try {
      window.sessionStorage.removeItem(frameCacheStorageKey(sessionId));
    } catch {
      // Ignore storage failures.
    }
  }
}

function renderUserMessage(value: string): string {
  if (!value.startsWith("{")) return value;
  try {
    const message = JSON.parse(value) as {
      type?: string;
      payload?: { name?: string; arg?: string; text?: string; script?: string };
      script?: string;
    };
    if (typeof message.payload?.text === "string") return message.payload.text;
    if (typeof message.payload?.script === "string") return message.payload.script;
    if (typeof message.script === "string") return message.script;
    if (
      (message.type !== "slash" && message.type !== "slash_command") ||
      !message.payload?.name
    ) {
      return value;
    }
    return slashCommandDisplayText(message.payload.name, message.payload.arg);
  } catch {
    return value;
  }
}

function isAgentMessageObject(message: AgentInboundMessage): message is AgentMessageObject {
  return typeof message === "object" && message !== null;
}

function slashCommandDisplayText(name: string, arg?: string): string {
  const command = name === "set-model" ? "model" : name;
  return `/${command}${arg ? ` ${arg}` : ""}`;
}

function displayTextForMessage(message: AgentInboundMessage): string {
  if (typeof message === "string") return message.trim();
  if (
    message.type === "slash" &&
    typeof message.payload === "object" &&
    message.payload != null &&
    "name" in message.payload &&
    typeof message.payload.name === "string"
  ) {
    const arg =
      "arg" in message.payload && typeof message.payload.arg === "string"
        ? message.payload.arg
        : undefined;
    return slashCommandDisplayText(message.payload.name, arg);
  }
  if (
    message.type === "run_script" &&
    typeof message.payload === "object" &&
    message.payload != null &&
    "script" in message.payload &&
    typeof message.payload.script === "string"
  ) {
    return message.payload.script.trim();
  }
  return JSON.stringify(message);
}

/**
 * The identity #ingestFrame dedupes on. A frame arriving twice is normal — a reconnect replays from
 * a root offset, and the cached frames overlap the live stream — so this has to say "same event"
 * exactly when it is the same event.
 *
 * An event read off a log reports its own offset there, which with the tree-unique `agent_id` is
 * precisely that: stable across redeliveries, distinct between two events of one agent. Prefer it.
 *
 * Fall back to hashing the payload only for the frames that have no offset to report — the ones the
 * server synthesized, plus client-side stream errors that carry no envelope. This fallback is what
 * every frame used to use, and the reason not to: two DIFFERENT events with byte-identical payloads
 * collide under it, and the second is silently dropped. Measured over 214 frames of live traffic
 * that never fired, but the synthesized `subagent_stream_unavailable` marker is constructibly
 * vulnerable — every field of it is a constant for a given child, so a child given up twice (the
 * merge re-arms a re-dispatched child's gate) yields two identical frames. Keeping the fallback
 * scoped to those frames holds their behavior exactly as it is today while real events, which are
 * the ones whose loss would corrupt the transcript, get an identity that cannot collide.
 */
function frameKey(frame: AgentSseFrame): string {
  if (
    "event_offset" in frame.data &&
    "agent_id" in frame.data &&
    typeof frame.data.event_offset === "number" &&
    frame.data.event_offset !== SYNTHESIZED
  ) {
    return `${frame.data.agent_id}|${frame.data.event_offset}`;
  }
  const identityData: Record<string, unknown> = { ...frame.data };
  delete identityData.resume_offset;
  delete identityData.event_offset;
  return `${frame.event}|${JSON.stringify(identityData)}`;
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

function isOperatorCommandFrame(frame: AgentSseFrame): boolean {
  return (
    frame.event === "operator_command_started" ||
    frame.event === "operator_command_completed" ||
    frame.event === "operator_command_failed"
  );
}

function isStopOperatorCommandName(name: string): boolean {
  return name === "stop-agent" || name === "stop";
}

export class AgentRunController {
  #api: AgentApi;
  #initialized = false;

  frames = $state<AgentSseFrame[]>([]);
  observedSubagents = $state<ObservedSubagent[]>([]);
  agentInterfaces = $state<Record<string, AgentInterfaceFunction[]>>({});
  operatorInterfaces = $state<Record<string, OperatorCommand[]>>({});
  closedWorkflowIds = $state<string[]>([]);
  viewIndex = $state(0);
  playing = $state(false);
  /**
   * Tail the live edge. Load-bearing now that the cursor only advances while it
   * is set: starting false would leave a fresh session parked at event zero
   * while frames streamed in behind it.
   */
  following = $state(true);
  connecting = $state(false);
  sending = $state(false);
  creatingSession = $state(false);
  refreshingSessions = $state(false);
  #connectionError = $state<string | null>(null);
  #connectionErrorCode = $state<string | null>(null);
  playbackSpeed = $state<PlaybackSpeed>(1);
  agents = $state<AgentDescriptor[]>([]);
  sessions = $state<Session[]>([]);
  session = $state<Session | null>(null);
  expectedTurn = $state(1);
  lastResumeOffset = $state(0);
  #streamVersion = 0;
  #connectionVersion = 0;
  #sendVersion = 0;
  #syncingSessions = false;
  #streamAbort: AbortController | null = null;
  #interfaceRequests = new Set<string>();
  #operatorInterfaceRequests = new Set<string>();
  #workflowResumeOffsets = new Map<string, number>();
  #workflowAttachAbort = new Map<string, AbortController>();
  #frameKeys = new Set<string>();
  #frameCacheTimer: number | null = null;
  /** Frames staged but not yet committed. Plain array: writing it must not react. */
  #frameBuffer: AgentSseFrame[] = [];
  #flushQueued = false;
  /** Bumped on session change, to strand a flush queued against the old session. */
  #publishGeneration = 0;
  /** A bounded backlog is being replayed in, so hold off on per-paint commits. */
  #catchingUp = false;
  /** A live frame has arrived on this stream, so the catch-up is over for good. */
  #liveFrameSeen = false;
  #catchUpStartedAt = 0;
  /** Frames staged since the last catch-up commit, counting toward the next chunk. */
  #sinceCatchUpPublish = 0;
  /** Deadline commit for a catch-up whose chunk may never fill. */
  #catchUpFlushTimer: number | null = null;
  #submitQueue: Promise<void> = Promise.resolve();
  #timer: number | null = null;

  /**
   * The last connection-level failure, and the machine-readable reason for it.
   *
   * `/api/attach` reports every way it can fail in band, as an error frame
   * carrying `kind`, `code` and `message` (see `_attach_error` and
   * `_unreplayable_run_frame` in `web/app.py`). Keeping only the sentence threw
   * away the one part a caller can branch on.
   *
   * Written through a setter rather than as a second public field because the
   * two must never disagree. Every existing assignment site is either a fresh
   * failure with no code of its own or a clearing, and a code left over from the
   * previous failure would be read as describing this one; clearing it here
   * means those sites stay correct without knowing this field exists. Only
   * #ingestFrame, which has the frame in hand, sets both.
   */
  get connectionError(): string | null {
    return this.#connectionError;
  }

  set connectionError(message: string | null) {
    this.#connectionError = message;
    this.#connectionErrorCode = null;
  }

  get connectionErrorCode(): string | null {
    return this.#connectionErrorCode;
  }

  /**
   * Whether this run's figures are unknown rather than zero.
   *
   * `unreplayable_run` means the run finished and Temporal cannot replay its
   * event stream, so this console holds none of the events it spent money on.
   * Every total derived from `frames` is therefore an empty sum, and rendering
   * one as `0 tok $0.0000` reports a measurement that was never taken — beside
   * runs whose zeros are real.
   */
  get runUnmeasured(): boolean {
    return this.#connectionErrorCode === "unreplayable_run";
  }

  constructor(api: AgentApi = new HttpAgentApi()) {
    this.#api = api;
    /* No teardown: one controller lives as long as the page (see
       createAgentRunController), and the handler is inert without a session. */
    if (typeof window !== "undefined") {
      window.addEventListener("online", this.#reattachWhenOnline);
    }
  }

  /**
   * Re-attach when connectivity comes back.
   *
   * The retry budget above deliberately stops asking, which strands a reader
   * whose outage outlasted it — and lengthening the array to cover a two minute
   * one is guessing at a number the browser already knows. This is the answer
   * to "is it worth asking again", so it does not need to be estimated.
   *
   * Doing nothing while a stream is in flight is what keeps an `online` event
   * from re-attaching a healthy stream underneath itself: #streamAbort is set
   * for exactly as long as an attach is running, and only #finishStream clears
   * it. So this fires for a stream that gave up, and not for one that rode the
   * blip out.
   */
  #reattachWhenOnline = (): void => {
    const session = this.session;
    if (this.#streamAbort || !session) return;
    if (this.#isWorkflowClosed(session.workflow_id)) return;
    this.connectionError = null;
    void this.attach(this.lastResumeOffset).catch((error: unknown) => {
      if (!isAbortError(error) && this.session?.workflow_id === session.workflow_id) {
        this.connectionError =
          error instanceof Error ? error.message : "Failed to reconnect.";
      }
    });
  };

  replayTimeline = $derived(this.#replayTimeline());
  visibleReplayTimeline = $derived(this.replayTimeline.slice(0, this.viewIndex));
  allReplayFrames = $derived(this.replayTimeline.map((entry) => entry.frame));
  visibleReplayFrames = $derived(
    this.visibleReplayTimeline.map((entry) => entry.frame)
  );
  visibleFrames = $derived(
    this.visibleReplayTimeline
      .filter((entry) => entry.role === "parent")
      .map((entry) => entry.frame)
  );
  currentFrame = $derived(
    this.viewIndex > 0 ? this.visibleReplayFrames.at(-1) ?? null : null
  );
  graphAgents = $derived(this.#graphAgents());
  graph = $derived(buildAgentTreeGraph(this.graphAgents));
  operatorTargets = $derived(this.#operatorTargets());
  sessionClosed = $derived(
    this.session != null && this.#isWorkflowClosed(this.session.workflow_id)
  );
  replayLog = $derived(buildReplayLog(this.visibleReplayTimeline));
  fullReplayLog = $derived(buildReplayLog(this.replayTimeline));
  chatTranscript = $derived(
    buildTranscript(
      this.replayTimeline
        .filter((entry) => entry.role === "parent" || isOperatorCommandFrame(entry.frame))
        .map((entry) => entry.frame)
    )
  );
  currentLogRow = $derived(
    this.fullReplayLog.rows.find((row) => row.index === this.viewIndex) ?? null
  );
  usage = $derived(summarizeCost(this.visibleReplayFrames));
  usageTimeline = $derived(buildUsageTimeline(this.allReplayFrames));
  stepTimeline = $derived(buildStepTimeline(this.replayTimeline));
  anomalyMarkers = $derived(buildReplayMarkers(this.replayTimeline));
  turnMarkers = $derived(
    this.replayTimeline
      .map((entry, index) =>
        entry.role === "parent" &&
        entry.frame.event === "turn_started" &&
        "type" in entry.frame.data
          ? { index, turnNumber: entry.frame.data.turn_number }
          : null
      )
      .filter((item): item is { index: number; turnNumber: number } => item != null)
  );

  get total(): number {
    // #replayTimeline() emits exactly one entry per frame, so this matches
    // replayTimeline.length without forcing that projection to rebuild. Reading
    // the projection here made appending one frame O(n), and hydrating a cached
    // session O(n^2) — 1,583 frames cost 10.2s of rebuilds before this.
    return this.session ? this.frames.length : 0;
  }

  get runInfo(): RunInfo {
    const session = this.session ?? realisticQaScenario.sessions[0];
    const agent = this.agents.find(
      (item) => item.workflow_type === session?.agent_workflow_type
    ) ?? realisticQaScenario.agents.find(
      (item) => item.workflow_type === session?.agent_workflow_type
    );
    return {
      sessionId: session?.workflow_id ?? "unknown-session",
      agentLabel: agent?.label ?? "Agent",
      models: summarizeCost(this.frames).modelBreakdown.map((item) => item.model),
      startedAt: session?.created_at ?? 0
    };
  }

  #beginConnection(): number {
    return ++this.#connectionVersion;
  }

  #isCurrentConnection(connectionVersion: number): boolean {
    return connectionVersion === this.#connectionVersion;
  }

  #replayTimeline(): ReplayTimelineEntry[] {
    const session = this.session;
    if (!session) return [];
    const observedBySubagentId = new Map(
      this.observedSubagents.map((agent) => [agent.subagentId, agent])
    );
    const parentTurnBySubagentTurn = new Map<string, number>();
    const timeline: ReplayTimelineEntry[] = [];

    for (const frame of this.frames) {
      if (!("type" in frame.data)) {
        timeline.push({
          workflowId: session.workflow_id,
          role: "parent",
          label: this.runInfo.agentLabel,
          frame
        });
        continue;
      }

      const observedSubagent = observedBySubagentId.get(frame.data.agent_id);
      const parentTurnNumber =
        observedSubagent == null
          ? undefined
          : parentTurnBySubagentTurn.get(
              `${frame.data.agent_id}:${frame.data.turn_number}`
            );
      const role: ReplayTimelineRole = observedSubagent == null ? "parent" : "subagent";
      timeline.push({
        workflowId: observedSubagent?.workflowId ?? session.workflow_id,
        role,
        label: observedSubagent?.label ?? this.runInfo.agentLabel,
        parentTurnNumber,
        frame
      });

      if (frame.event === "subagent_message_sent") {
        const enclosingParentTurn =
          role === "subagent" && parentTurnNumber != null
            ? parentTurnNumber
            : frame.data.turn_number;
        parentTurnBySubagentTurn.set(
          `${frame.data.subagent_id}:${frame.data.subagent_turn}`,
          enclosingParentTurn
        );
      }
    }

    if (import.meta.env.DEV && timeline.length !== this.frames.length) {
      console.error(
        `replayTimeline emitted ${timeline.length} entries for ${this.frames.length} frames. ` +
          "get total() returns frames.length to avoid rebuilding this projection on every " +
          "appended frame, and that shortcut is now wrong."
      );
    }
    return timeline;
  }

  #graphAgents(): AgentGraphSource[] {
    const session = this.session;
    if (!session) return [];
    const visibleSubagentWorkflowIds = new Set<string>();
    const visibleSubagentFrames = new Map<string, AgentSseFrame[]>();
    for (const frame of this.visibleFrames) {
      if (!("type" in frame.data)) continue;
      if (
        frame.event !== "subagent_started" &&
        frame.event !== "subagent_message_sent" &&
        frame.event !== "subagent_reply_received" &&
        frame.event !== "subagent_stopped" &&
        frame.event !== "subagent_stream_unavailable"
      ) {
        continue;
      }
      const workflowId = "workflow_id" in frame.data ? frame.data.workflow_id : null;
      if (!workflowId) continue;
      visibleSubagentWorkflowIds.add(workflowId);
    }
    for (const entry of this.visibleReplayTimeline) {
      if (entry.role !== "subagent") continue;
      visibleSubagentWorkflowIds.add(entry.workflowId);
      const frames = visibleSubagentFrames.get(entry.workflowId) ?? [];
      frames.push(entry.frame);
      visibleSubagentFrames.set(entry.workflowId, frames);
    }
    for (const agent of this.observedSubagents) {
      if (visibleSubagentFrames.has(agent.workflowId)) {
        visibleSubagentWorkflowIds.add(agent.workflowId);
      }
    }
    return [
      {
        workflowId: session.workflow_id,
        role: "parent",
        label: this.runInfo.agentLabel,
        frames: this.visibleFrames,
        agentInterface: this.agentInterfaces[session.workflow_id] ?? []
      },
      ...this.observedSubagents
        .filter((agent) => visibleSubagentWorkflowIds.has(agent.workflowId))
        .map((agent) => ({
          workflowId: agent.workflowId,
          role: agent.role,
          label: agent.label,
          parentWorkflowId: agent.parentWorkflowId,
          subagentId: agent.subagentId,
          agentKey: agent.agentKey,
          frames: visibleSubagentFrames.get(agent.workflowId) ?? [],
          agentInterface:
            this.agentInterfaces[agent.workflowId] ?? agent.agentInterface ?? [],
          operatorInterface:
            this.operatorInterfaces[agent.workflowId] ?? agent.operatorInterface ?? [],
          stopped: agent.stopped || this.#isWorkflowClosed(agent.workflowId)
        }))
    ];
  }

  #stopStream(): void {
    this.#streamAbort?.abort();
    this.#streamAbort = null;
    this.#streamVersion += 1;
  }

  #beginStream(): {
    controller: AbortController;
    signal: AbortSignal;
    streamVersion: number;
  } {
    this.#streamAbort?.abort();
    const controller = new AbortController();
    this.#streamAbort = controller;
    /* A fresh attach re-opens on a backlog, so it gets a catch-up of its own —
       the latch is per stream, not per session. */
    this.#liveFrameSeen = false;
    return {
      controller,
      signal: controller.signal,
      streamVersion: ++this.#streamVersion
    };
  }

  #finishStream(controller: AbortController): void {
    if (this.#streamAbort === controller) this.#streamAbort = null;
  }

  #stopWorkflowAttach(workflowId: string): void {
    const controller = this.#workflowAttachAbort.get(workflowId);
    controller?.abort();
    this.#workflowAttachAbort.delete(workflowId);
  }

  #stopWorkflowAttachStreams(): void {
    for (const controller of this.#workflowAttachAbort.values()) {
      controller.abort();
    }
    this.#workflowAttachAbort.clear();
  }

  #isKnownWorkflowId(workflowId: string): boolean {
    return (
      workflowId === this.session?.workflow_id ||
      this.observedSubagents.some((agent) => agent.workflowId === workflowId)
    );
  }

  #markWorkflowClosed(workflowId: string): void {
    this.sessions = this.sessions.map((session) =>
      session.workflow_id === workflowId
        ? {
            ...session,
            execution_status: session.execution_status ?? "COMPLETED",
            closed: true
          }
        : session
    );
    if (this.session?.workflow_id === workflowId) {
      this.session = {
        ...this.session,
        execution_status: this.session.execution_status ?? "COMPLETED",
        closed: true
      };
    }
    if (this.closedWorkflowIds.includes(workflowId)) return;
    this.closedWorkflowIds = [...this.closedWorkflowIds, workflowId];
    if (workflowId === this.session?.workflow_id) {
      this.#stopStream();
    } else {
      this.#stopWorkflowAttach(workflowId);
    }
  }

  #markObservedSubagentStopped(workflowId: string): void {
    this.observedSubagents = this.observedSubagents.map((agent) =>
      agent.workflowId === workflowId ? { ...agent, stopped: true } : agent
    );
  }

  #isWorkflowClosed(workflowId: string): boolean {
    return (
      this.closedWorkflowIds.includes(workflowId) ||
      this.session?.workflow_id === workflowId && Boolean(this.session.closed) ||
      this.sessions.some((session) => session.workflow_id === workflowId && session.closed)
    );
  }

  #applyWorkflowExecutionState(state: WorkflowExecutionState): void {
    this.sessions = this.sessions.map((session) =>
      session.workflow_id === state.workflow_id
        ? {
            ...session,
            execution_status: state.execution_status,
            closed: state.closed
          }
        : session
    );
    if (this.session?.workflow_id === state.workflow_id) {
      this.session = {
        ...this.session,
        execution_status: state.execution_status,
        closed: state.closed
      };
    }
    if (state.closed) this.#markWorkflowClosed(state.workflow_id);
  }

  #applySessionExecutionStates(sessions: Session[]): void {
    for (const session of sessions) {
      if (session.closed) {
        this.#markWorkflowClosed(session.workflow_id);
      }
    }
  }

  async #refreshWorkflowExecutionState(workflowId: string): Promise<void> {
    const state = await this.#api.workflowStatus(workflowId);
    this.#applyWorkflowExecutionState(state);
  }

  #resumeOffsetForWorkflow(workflowId: string): number {
    if (workflowId === this.session?.workflow_id) return this.lastResumeOffset;
    return this.#workflowResumeOffsets.get(workflowId) ?? 0;
  }

  #operatorTargets(): OperatorTarget[] {
    const session = this.session;
    if (!session) return [];
    return [
      {
        workflowId: session.workflow_id,
        role: "parent",
        label: this.runInfo.agentLabel,
        operatorInterface: this.operatorInterfaces[session.workflow_id] ?? [],
        closed: this.#isWorkflowClosed(session.workflow_id)
      },
      ...this.observedSubagents
        .map((agent) => ({
          workflowId: agent.workflowId,
          role: agent.role,
          label: agent.label,
          operatorInterface:
            this.operatorInterfaces[agent.workflowId] ??
            agent.operatorInterface ??
            [],
          closed: agent.stopped || this.#isWorkflowClosed(agent.workflowId)
        }))
    ];
  }

  operatorTargetForWorkflow(workflowId?: string | null): OperatorTarget | null {
    const session = this.session;
    if (!session) return null;
    if (!workflowId || workflowId === session.workflow_id) {
      return {
        workflowId: session.workflow_id,
        role: "parent",
        label: this.runInfo.agentLabel,
        operatorInterface: this.operatorInterfaces[session.workflow_id] ?? [],
        closed: this.#isWorkflowClosed(session.workflow_id)
      };
    }

    const subagent = this.observedSubagents.find(
      (agent) => agent.workflowId === workflowId
    );
    if (!subagent) {
      return {
        workflowId: session.workflow_id,
        role: "parent",
        label: this.runInfo.agentLabel,
        operatorInterface: this.operatorInterfaces[session.workflow_id] ?? [],
        closed: this.#isWorkflowClosed(session.workflow_id)
      };
    }

    return {
      workflowId: subagent.workflowId,
      role: "subagent",
      label: subagent.label,
      operatorInterface:
        this.operatorInterfaces[subagent.workflowId] ??
        subagent.operatorInterface ??
        [],
      closed: subagent.stopped || this.#isWorkflowClosed(subagent.workflowId)
    };
  }

  #subagentLabel(agentKey: string, subagentId: string): string {
    const descriptor = this.agents.find((agent) => agent.key === agentKey);
    return `${descriptor?.label ?? agentKey} (${subagentId})`;
  }

  #upsertSubagent(data: {
    workflow_id: string;
    subagent_id: string;
    agent_key?: string;
    targetTurn?: number;
    stopped?: boolean;
  }, parentWorkflowId = this.session?.workflow_id): void {
    if (!parentWorkflowId) return;
    const existing = this.observedSubagents.find(
      (agent) => agent.workflowId === data.workflow_id
    );
    const agentKey = data.agent_key ?? existing?.agentKey ?? "subagent";
    const next: ObservedSubagent = {
      workflowId: data.workflow_id,
      role: "subagent",
      parentWorkflowId,
      subagentId: data.subagent_id,
      agentKey,
      label: this.#subagentLabel(agentKey, data.subagent_id),
      agentInterface:
        this.agentInterfaces[data.workflow_id] ?? existing?.agentInterface,
      operatorInterface:
        this.operatorInterfaces[data.workflow_id] ?? existing?.operatorInterface,
      targetTurn:
        data.targetTurn == null
          ? existing?.targetTurn ?? null
          : Math.max(existing?.targetTurn ?? 0, data.targetTurn),
      stopped: data.stopped ?? existing?.stopped ?? false
    };
    this.observedSubagents = [
      ...this.observedSubagents.filter((agent) => agent.workflowId !== data.workflow_id),
      next
    ];
  }

  async #fetchAgentInterface(workflowId: string): Promise<void> {
    if (this.agentInterfaces[workflowId] || this.#interfaceRequests.has(workflowId)) {
      return;
    }
    this.#interfaceRequests.add(workflowId);
    try {
      const agentInterface = await this.#api.agentInterface(workflowId);
      this.agentInterfaces = {
        ...this.agentInterfaces,
        [workflowId]: agentInterface
      };
      if (this.observedSubagents.some((agent) => agent.workflowId === workflowId)) {
        this.observedSubagents = this.observedSubagents.map((agent) =>
          agent.workflowId === workflowId ? { ...agent, agentInterface } : agent
        );
      }
    } catch {
      // Agent-interface discovery is auxiliary UI metadata; streaming remains authoritative.
    } finally {
      this.#interfaceRequests.delete(workflowId);
    }
  }

  async #fetchOperatorInterface(workflowId: string): Promise<void> {
    if (
      this.operatorInterfaces[workflowId] ||
      this.#operatorInterfaceRequests.has(workflowId)
    ) {
      return;
    }
    this.#operatorInterfaceRequests.add(workflowId);
    try {
      const operatorInterface = await this.#api.operatorInterface(workflowId);
      this.operatorInterfaces = {
        ...this.operatorInterfaces,
        [workflowId]: operatorInterface
      };
      if (this.observedSubagents.some((agent) => agent.workflowId === workflowId)) {
        this.observedSubagents = this.observedSubagents.map((agent) =>
          agent.workflowId === workflowId ? { ...agent, operatorInterface } : agent
        );
      }
    } catch {
      // Operator-interface discovery is auxiliary UI metadata; streaming remains authoritative.
    } finally {
      this.#operatorInterfaceRequests.delete(workflowId);
    }
  }

  async initialize(): Promise<void> {
    if (this.#initialized) return;
    this.#initialized = true;
    const connectionVersion = this.#beginConnection();
    this.connecting = true;
    this.connectionError = null;

    try {
      const agents = await this.#loadAgents();
      const defaultAgent = agents.find((agent) => agent.key === "qa") ?? agents[0];
      if (!defaultAgent) throw new Error("No agent is registered.");

      const sessions = await this.#api.listSessions();
      this.sessions = sessions;
      this.#applySessionExecutionStates(sessions);
      const storedSessionId = readStoredActiveSessionId();
      const openable = chooseBootSession(
        sessions,
        storedSessionId,
        defaultAgent.workflow_type
      );

      if (openable) {
        this.session = openable;
      } else {
        this.session = await this.#api.createSession({
          agent_workflow_type: defaultAgent.workflow_type,
          is_message_queuing_enabled: true
        });
        this.sessions = [...this.sessions, this.session];
      }
      writeStoredActiveSessionId(this.session.workflow_id);
      void this.#fetchAgentInterface(this.session.workflow_id);
      void this.#fetchOperatorInterface(this.session.workflow_id);
      /* Awaited so the cache lands before the live stream opens: interleaving
         the two would order the buffer by arrival rather than by event. */
      await this.#hydrateCachedFrames(this.session.workflow_id);
      await this.#refreshWorkflowExecutionState(this.session.workflow_id);

      if (!this.#isCurrentConnection(connectionVersion)) return;
      if (this.#isWorkflowClosed(this.session.workflow_id)) return;
      await this.attach(this.lastResumeOffset);
    } catch (error) {
      if (this.#isCurrentConnection(connectionVersion) && !isAbortError(error)) {
        this.connectionError =
          error instanceof Error ? error.message : "Failed to initialize agent session.";
      }
    } finally {
      if (this.#isCurrentConnection(connectionVersion)) this.connecting = false;
    }
  }

  async #loadSessions(): Promise<void> {
    const sessions = await this.#api.listSessions();
    this.sessions = sessions;
    this.#applySessionExecutionStates(sessions);
  }

  async refreshSessions(): Promise<void> {
    if (this.refreshingSessions) return;
    this.refreshingSessions = true;
    try {
      await this.#loadSessions();
    } catch (error) {
      this.connectionError =
        error instanceof Error ? error.message : "Failed to refresh sessions.";
    } finally {
      this.refreshingSessions = false;
    }
  }

  /**
   * Re-read the session list on the reader's behalf rather than at their request.
   *
   * Anything holding a Temporal client can start a session, so the list this UI
   * created is only ever part of the picture. Quiet on purpose: a tick nobody
   * asked for must not spin the refresh control or raise the connection banner
   * over a blip the next tick would have covered.
   *
   * A tick is skipped while either read is still in flight. `/api/sessions` costs
   * a describe plus a history scan per session and has been measured at twelve
   * seconds against a registry of twenty stale entries, so a sync outlasting the
   * ten-second interval is the expected case, not the pathological one; without
   * this the ticks would overlap and pile up on a server already struggling.
   */
  async syncSessions(): Promise<void> {
    if (this.refreshingSessions || this.#syncingSessions) return;
    this.#syncingSessions = true;
    try {
      await this.#loadSessions();
    } catch {
      // The list stays as it was until a later tick answers.
    } finally {
      this.#syncingSessions = false;
    }
  }

  async startNewSession(workflowType?: string): Promise<void> {
    const connectionVersion = this.#beginConnection();
    this.#sendVersion += 1;
    this.#stopStream();
    this.sending = false;
    this.creatingSession = true;
    this.connecting = true;
    this.connectionError = null;

    try {
      const agents = await this.#loadAgents();
      const currentWorkflowType = this.session?.agent_workflow_type;
      const agent =
        agents.find((item) => item.workflow_type === workflowType) ??
        agents.find((item) => item.workflow_type === currentWorkflowType) ??
        agents.find((item) => item.key === "qa") ??
        agents[0];

      if (!agent) throw new Error("No agent is registered.");

      const session = await this.#api.createSession({
        agent_workflow_type: agent.workflow_type,
        is_message_queuing_enabled: true
      });

      this.sessions = [...this.sessions.filter((item) => item.workflow_id !== session.workflow_id), session];
      if (!this.#isCurrentConnection(connectionVersion)) return;

      this.#initialized = true;
      this.#resetSessionView();
      this.session = session;
      writeStoredActiveSessionId(session.workflow_id);
      void this.#fetchAgentInterface(session.workflow_id);
      void this.#fetchOperatorInterface(session.workflow_id);
      await this.#refreshWorkflowExecutionState(session.workflow_id);
      if (!this.#isCurrentConnection(connectionVersion)) return;
      if (this.#isWorkflowClosed(session.workflow_id)) return;
      await this.attach(0);
    } catch (error) {
      if (this.#isCurrentConnection(connectionVersion) && !isAbortError(error)) {
        this.connectionError =
          error instanceof Error ? error.message : "Failed to create agent session.";
      }
    } finally {
      if (this.#isCurrentConnection(connectionVersion)) {
        this.creatingSession = false;
        this.connecting = false;
      }
    }
  }

  async selectSession(sessionId: string): Promise<void> {
    if (this.session?.workflow_id === sessionId) {
      writeStoredActiveSessionId(sessionId);
      return;
    }
    const session = this.sessions.find((item) => item.workflow_id === sessionId);
    if (!session) return;

    const connectionVersion = this.#beginConnection();
    this.#sendVersion += 1;
    this.creatingSession = false;
    this.connecting = true;
    this.sending = false;
    this.connectionError = null;
    this.#resetSessionView();
    this.session = session;
    writeStoredActiveSessionId(session.workflow_id);
    void this.#fetchAgentInterface(session.workflow_id);
    void this.#fetchOperatorInterface(session.workflow_id);
    await this.#hydrateCachedFrames(session.workflow_id);

    try {
      await this.#refreshWorkflowExecutionState(session.workflow_id);
      if (!this.#isCurrentConnection(connectionVersion)) return;
      if (this.#isWorkflowClosed(session.workflow_id)) return;
      await this.attach(this.lastResumeOffset);
    } catch (error) {
      if (this.#isCurrentConnection(connectionVersion) && !isAbortError(error)) {
        this.connectionError =
          error instanceof Error ? error.message : "Failed to load selected session.";
      }
    } finally {
      if (this.#isCurrentConnection(connectionVersion)) this.connecting = false;
    }
  }

  /**
   * Whether a stream that just stopped carrying frames was dropped rather than
   * finished.
   *
   * The generator ends cleanly either way, so the stream ending is evidence of
   * neither — /api/attach has no marker for the root workflow dying. Temporal is
   * the thing that knows: a workflow still running has more to say, so a stream
   * that stopped carrying it was dropped. When an in-band error frame does start
   * arriving it needs no special case here, because a root that died reports as
   * closed and stops the retry on its own; the frame supplies the message.
   *
   * A status call that fails is the transient case this exists for, so it counts
   * as worth retrying. The retry budget is what keeps an outage bounded.
   *
   * `workflow_not_found` is the exception: the server has said the history is
   * deleted or past retention, so there is nothing left to stream and no wait
   * will produce any. Spending the budget on it is guaranteed-futile work, and
   * each attempt re-appends the same error frame.
   */
  async #streamDroppedMidRun(workflowId: string): Promise<boolean> {
    if (this.#isWorkflowClosed(workflowId)) return false;
    if (this.#connectionErrorCode === "workflow_not_found") return false;
    try {
      await this.#refreshWorkflowExecutionState(workflowId);
    } catch {
      return true;
    }
    return !this.#isWorkflowClosed(workflowId);
  }

  async attach(
    fromOffset = this.lastResumeOffset,
    options: { clearSendingOnIdle?: boolean } = {}
  ): Promise<void> {
    const session = this.session;
    if (!session) return;

    /* One #beginStream for the whole attachment, retries included, so every
       attempt shares its abort controller and stream version. A session switch
       then aborts the in-flight read and the backoff sleep together, and no
       retry can outlive the stream it belongs to. */
    const { controller, signal, streamVersion } = this.#beginStream();
    const isCurrentStream = (): boolean =>
      streamVersion === this.#streamVersion &&
      this.session?.workflow_id === session.workflow_id;
    let offset = Math.max(0, fromOffset);
    let attempt = 0;
    try {
      while (isCurrentStream()) {
        let delivered = false;
        try {
          for await (const frame of this.#api.attach(session.workflow_id, offset, signal)) {
            if (!isCurrentStream()) break;
            delivered = true;
            this.#appendFrame(frame);
          }
        } catch (error) {
          if (isAbortError(error) || !isCurrentStream()) break;
          /* Out of retries: surface the transport error the way an un-retried
             attach always did, rather than ending quietly. */
          if (attempt >= reattachBackoffMs.length) throw error;
        }
        /* Publish before asking why the stream stopped. Finding out costs a
           status call, and learning the workflow closed stops the stream
           (#markWorkflowClosed -> #stopStream), which bumps the stream version
           and makes the flush below skip its own guard — stranding exactly the
           tail it exists to commit.

           Keyed on the session rather than the stream version, because a bumped
           version is the condition being worked around. A switched session is
           not: its buffer belongs to someone else now. */
        if (this.session?.workflow_id === session.workflow_id) this.#flushStreamTail();
        /* The transcript is on screen, so the reader is no longer waiting on us
           — whatever the retries do from here is background liveness. Held
           across the whole loop, this reported "connecting" for the full 31.5s
           of backoff on a session that had finished loading in ten
           milliseconds, which is what "sessions load slowly" was.

           Cleared here rather than by the callers, because they await this
           method: selectSession's own `finally` cannot run until the last retry
           has. Guarded on the stream, so a session switched away from mid-retry
           does not clear the flag the new session just set. */
        if (isCurrentStream()) this.connecting = false;
        /* A stream that carried something earned a fresh budget, so hours of
           occasional blips do not add up to an exhausted one. */
        if (delivered) attempt = 0;
        if (attempt >= reattachBackoffMs.length) break;
        if (!(await this.#streamDroppedMidRun(session.workflow_id))) break;
        await sleepUnlessAborted(reattachBackoffMs[attempt], signal);
        attempt += 1;
        /* Resume only from an offset the server already proved it holds, by
           having sent it. An offset past the end answers 200 and then hangs
           open forever, so inventing one trades a quiet console for a wedged
           one. */
        offset = Math.max(offset, this.lastResumeOffset);
      }
    } catch (error) {
      if (!isAbortError(error)) throw error;
    } finally {
      if (
        streamVersion === this.#streamVersion &&
        this.session?.workflow_id === session.workflow_id
      ) {
        if (options.clearSendingOnIdle) this.sending = false;
        this.#flushStreamTail();
      }
      this.#finishStream(controller);
    }
  }

  /**
   * Commit whatever the stream staged but never published.
   *
   * A stream can stop mid-catch-up. #schedulePublish only commits at a chunk
   * boundary past the ceiling, so a replay that stops short of one strands its
   * tail with nothing left to flush it, and an idle session — whose whole
   * history is replay and which never crosses to live — shows an empty console.
   * Worth doing on the error path too: partial history beats nothing.
   */
  #flushStreamTail(): void {
    this.#catchingUp = false;
    this.#sinceCatchUpPublish = 0;
    /* Same invariant #armCatchUpFlush relies on: `frames` is a whole copy of the
       buffer and the buffer only grows, so equal lengths mean nothing new is
       staged, and committing would rebuild every projection to reproduce the
       array already on screen. */
    if (this.#frameBuffer.length === this.frames.length) return;
    this.#publishFrames();
  }

  async #attachWorkflow(
    workflowId: string,
    fromOffset = this.#resumeOffsetForWorkflow(workflowId)
  ): Promise<void> {
    const session = this.session;
    if (!session || !this.#isKnownWorkflowId(workflowId)) return;

    this.#stopWorkflowAttach(workflowId);
    const controller = new AbortController();
    this.#workflowAttachAbort.set(workflowId, controller);
    try {
      for await (const frame of this.#api.attach(
        workflowId,
        fromOffset,
        controller.signal
      )) {
        if (
          controller.signal.aborted ||
          this.session?.workflow_id !== session.workflow_id ||
          !this.#isKnownWorkflowId(workflowId)
        ) {
          break;
        }
        this.#appendFrame(frame, { sourceWorkflowId: workflowId });
      }
    } catch (error) {
      if (!isAbortError(error)) throw error;
    } finally {
      if (this.#workflowAttachAbort.get(workflowId) === controller) {
        this.#workflowAttachAbort.delete(workflowId);
        /* Same stranded tail as the root stream: a subagent's history is replay
           too, and its stream ends without ever crossing to live. Only flush
           while this is still the stream that owns the buffer, and still the
           session it was opened for. */
        if (this.session?.workflow_id === session.workflow_id) {
          this.#flushStreamTail();
        }
      }
    }
  }

  async sendMessage(message: AgentInboundMessage): Promise<void> {
    const displayText = displayTextForMessage(message);
    if (!displayText) return;
    await this.initialize();
    const session = this.session;
    if (!session) return;
    try {
      await this.#refreshWorkflowExecutionState(session.workflow_id);
    } catch (error) {
      this.connectionError =
        error instanceof Error
          ? error.message
          : "Failed to check workflow status.";
      return;
    }
    if (this.#isWorkflowClosed(session.workflow_id)) {
      this.connectionError = null;
      this.sending = false;
      return;
    }

    this.pause();
    const expectedTurn = this.expectedTurn;
    this.expectedTurn += 1;
    ++this.#sendVersion;
    this.sending = true;
    this.connectionError = null;
    this.#recordInitialUserMessage(displayText);

    const submitted = this.#submitQueue.then(async () => {
      if (this.session?.workflow_id !== session.workflow_id) return;
      await this.#api.submitMessage({
        session_id: session.workflow_id,
        message: this.#messageForSession(message, session),
        expected_turn: expectedTurn
      });
    });
    this.#submitQueue = submitted.catch(() => {});

    try {
      await submitted;
      if (this.session?.workflow_id !== session.workflow_id) return;
      void this.attach(this.lastResumeOffset, { clearSendingOnIdle: true }).catch(
        (error: unknown) => {
          if (!isAbortError(error) && this.session?.workflow_id === session.workflow_id) {
            this.connectionError =
              error instanceof Error ? error.message : "Failed to stream messages.";
            this.sending = false;
          }
        }
      );
    } catch (error) {
      if (isAbortError(error) || this.session?.workflow_id !== session.workflow_id) {
        return;
      }
      this.expectedTurn = Math.max(1, expectedTurn);
      this.connectionError =
        error instanceof Error ? error.message : "Failed to send message.";
      this.sending = false;
      await this.attach(this.lastResumeOffset);
    }
  }

  async executeOperatorCommand(
    name: string,
    arg?: string | null,
    workflowId?: string | null
  ): Promise<OperatorCommandResponse> {
    await this.initialize();
    const session = this.session;
    if (!session) throw new Error("No active session.");
    const targetWorkflowId =
      workflowId && this.#isKnownWorkflowId(workflowId)
        ? workflowId
        : session.workflow_id;

    this.connectionError = null;
    try {
      if (!isStopOperatorCommandName(name)) {
        await this.#refreshWorkflowExecutionState(targetWorkflowId);
        if (this.#isWorkflowClosed(targetWorkflowId)) {
          return { text: "Agent is closed." };
        }
      }
      const result = await this.#api.executeOperatorCommand({
        session_id: targetWorkflowId,
        name,
        arg: arg ?? null
      });
      if (isStopOperatorCommandName(name)) {
        this.#markWorkflowClosed(targetWorkflowId);
        if (targetWorkflowId === session.workflow_id) {
          this.sending = false;
        } else {
          this.#markObservedSubagentStopped(targetWorkflowId);
        }
        return result;
      }
      if (targetWorkflowId === session.workflow_id) {
        const shouldClearSendingOnIdle = this.sending;
        void this.attach(this.lastResumeOffset, {
          clearSendingOnIdle: shouldClearSendingOnIdle
        }).catch((error: unknown) => {
          if (!isAbortError(error) && this.session?.workflow_id === session.workflow_id) {
            this.connectionError =
              error instanceof Error ? error.message : "Failed to stream operator events.";
            if (shouldClearSendingOnIdle) this.sending = false;
          }
        });
      } else {
        void this.#attachWorkflow(
          targetWorkflowId,
          this.#resumeOffsetForWorkflow(targetWorkflowId)
        ).catch((error: unknown) => {
          if (!isAbortError(error) && this.session?.workflow_id === session.workflow_id) {
            this.connectionError =
              error instanceof Error
                ? error.message
                : "Failed to stream operator events.";
          }
        });
      }
      return result;
    } catch (error) {
      this.connectionError =
        error instanceof Error ? error.message : "Failed to execute operator command.";
      throw error;
    }
  }

  async approveTool(
    workflowId: string,
    toolId: string,
    approved: boolean,
    remember = false
  ): Promise<void> {
    const session = this.session;
    if (!session) throw new Error("No active session.");
    if (!this.#isKnownWorkflowId(workflowId)) {
      throw new Error("Cannot resolve approval for an unknown agent workflow.");
    }

    this.connectionError = null;
    try {
      await this.#api.approve({
        session_id: workflowId,
        tool_id: toolId,
        approved,
        reason: approved ? null : "Rejected in chat.",
        remember: approved && remember
      });
    } catch (error) {
      this.connectionError =
        error instanceof Error ? error.message : "Failed to resolve tool approval.";
      throw error;
    }
  }

  async #loadAgents(): Promise<AgentDescriptor[]> {
    if (this.agents.length > 0) return this.agents;
    const { agents } = await this.#api.listAgents();
    this.agents = agents;
    return agents;
  }

  #messageForSession(message: AgentInboundMessage, session: Session): AgentInboundMessage {
    if (isAgentMessageObject(message)) return message;
    if (session.agent_workflow_type === "MontyDynamicAgent") {
      return { type: "run_script", payload: { script: message } };
    }
    return message;
  }

  #recordInitialUserMessage(message: string): void {
    const session = this.session;
    if (!session) return;
    this.sessions = this.sessions.map((item) =>
      item.workflow_id === session.workflow_id && !item.initial_user_message
        ? { ...item, initial_user_message: message }
        : item
    );
    if (!session.initial_user_message) {
      this.session = { ...session, initial_user_message: message };
    }
  }

  /**
   * Replay the cached frames for a session back into the buffer.
   *
   * The one place the pipeline does its own chunking. Everywhere else frames
   * arrive from an await, so the event loop breathes between them by itself;
   * here the whole cache is already in hand and a tight loop over it would hold
   * the main thread for the length of the session.
   */
  async #hydrateCachedFrames(sessionId: string): Promise<void> {
    const cachedFrames = readCachedFrames(sessionId);
    if (cachedFrames.length === 0) return;
    this.#catchingUp = true;
    this.#catchUpStartedAt = now();
    try {
      for (let index = 0; index < cachedFrames.length; index += 1) {
        if (this.session?.workflow_id !== sessionId) return;
        this.#ingestFrame(cachedFrames[index], { persist: false });
        if ((index + 1) % framePublishChunkSize !== 0) continue;
        if (publishAtChunkBoundary(this.#catchingUp, now() - this.#catchUpStartedAt)) {
          this.#publishFrames();
          /* Restarting the clock is what makes the ceiling a rate limit rather
             than just a delay: without it, every chunk past the first second
             commits, and chunks can pass far faster than the page can paint. */
          this.#catchUpStartedAt = now();
        }
        await yieldToMain();
      }
    } finally {
      this.#catchingUp = false;
      this.#publishFrames();
    }
  }

  #scheduleFrameCacheWrite(): void {
    const sessionId = this.session?.workflow_id;
    if (!sessionId || typeof window === "undefined") return;
    if (this.#frameCacheTimer != null) return;
    this.#frameCacheTimer = window.setTimeout(() => {
      this.#frameCacheTimer = null;
      if (this.session?.workflow_id !== sessionId) return;
      writeCachedFrames(sessionId, this.frames);
    }, 250);
  }

  #resetSessionView(): void {
    this.pause();
    this.#stopStream();
    this.#stopWorkflowAttachStreams();
    this.frames = [];
    this.observedSubagents = [];
    this.#frameKeys = new Set<string>();
    this.#frameBuffer = [];
    /* Strand any flush already queued: it would republish the old session's
       buffer over the new session's empty one. */
    this.#publishGeneration += 1;
    this.#flushQueued = false;
    this.#clearCatchUpFlush();
    this.#catchingUp = false;
    this.#liveFrameSeen = false;
    this.#sinceCatchUpPublish = 0;
    this.#workflowResumeOffsets = new Map<string, number>();
    this.viewIndex = 0;
    this.following = true;
    this.expectedTurn = 1;
    this.lastResumeOffset = 0;
  }

  /**
   * Stage one frame: dedup it, record its bookkeeping, and buffer it.
   *
   * Deliberately does not touch `frames`. Committing is what costs — it re-runs
   * every derived projection — so it happens per batch in #publishFrames().
   */
  #ingestFrame(
    frame: AgentSseFrame,
    options: { persist?: boolean; sourceWorkflowId?: string } = {}
  ): void {
    const key = frameKey(frame);
    if (this.#frameKeys.has(key)) return;
    this.#frameKeys.add(key);

    if (!("type" in frame.data)) {
      this.#connectionError = frame.data.message;
      this.#connectionErrorCode =
        "code" in frame.data && typeof frame.data.code === "string"
          ? frame.data.code
          : null;
    }
    const publisherWorkflowId =
      this.#publisherWorkflowId(frame) ?? options.sourceWorkflowId;
    const isRootFrame = publisherWorkflowId === this.session?.workflow_id;

    this.#frameBuffer.push(frame);

    if (
      "resume_offset" in frame.data &&
      typeof frame.data.resume_offset === "number"
    ) {
      const resumeOffsetOwner =
        options.sourceWorkflowId ?? (isRootFrame ? publisherWorkflowId : undefined);
      if (resumeOffsetOwner) {
        this.#workflowResumeOffsets.set(
          resumeOffsetOwner,
          Math.max(
            this.#workflowResumeOffsets.get(resumeOffsetOwner) ?? 0,
            frame.data.resume_offset
          )
        );
      }
      if (isRootFrame) {
        this.lastResumeOffset = Math.max(
          this.lastResumeOffset,
          frame.data.resume_offset
        );
      }
    }
    if (
      isRootFrame &&
      "type" in frame.data &&
      frame.data.turn_number >= this.expectedTurn
    ) {
      this.expectedTurn = frame.data.turn_number + 1;
    }
    if (isRootFrame && frame.event === "turn_started" && frame.data.turn_number === 1) {
      this.#recordInitialUserMessage(renderUserMessage(frame.data.user_message));
    }
    if (
      publisherWorkflowId &&
      frame.event === "operator_command_completed" &&
      "type" in frame.data &&
      isStopOperatorCommandName(frame.data.command_name)
    ) {
      this.#markWorkflowClosed(publisherWorkflowId);
      if (!isRootFrame) this.#markObservedSubagentStopped(publisherWorkflowId);
      if (isRootFrame) this.sending = false;
    }
    this.#handleSubagentEvent(frame, publisherWorkflowId);
    if (options.persist !== false) this.#scheduleFrameCacheWrite();
  }

  /**
   * Commit everything staged so far, in one reactive write.
   *
   * ponytail: a commit is O(frames) and stays that way — batching bounded how
   * MANY commits happen, not what one costs. The copy below is not the cost
   * (0.01% of a commit, measured); the derived projections rebuilding from
   * scratch are, at ~5us per frame across all of them, so one commit is ~27ms at
   * 5,000 events and ~107ms at 20,000. Ceiling: total work is still
   * (commits x frames), so a long LIVE session — one commit per paint — is
   * quadratic, and past a few thousand events every arriving event costs a
   * visible hitch. Upgrading that means making the projections incremental
   * (append one entry, don't rebuild the timeline), which is a state-layer
   * change, not a cheaper copy here.
   */
  #publishFrames(): void {
    this.#clearCatchUpFlush();
    this.frames = this.#frameBuffer.slice();
    this.viewIndex = cursorAfterPublish(this.following, this.viewIndex, this.total);
  }

  #clearCatchUpFlush(): void {
    if (this.#catchUpFlushTimer == null) return;
    clearTimeout(this.#catchUpFlushTimer);
    this.#catchUpFlushTimer = null;
  }

  /**
   * Commit the staged backlog once the ceiling passes, even if its chunk never
   * fills.
   *
   * The chunk schedule is driven entirely by frames arriving — #schedulePublish
   * runs from #appendFrame and nowhere else — so a stream that stays open while
   * trickling fewer than a chunk's worth of replay frames publishes nothing, and
   * waiting does not help. That is a live session someone is watching, showing a
   * blank console. A deadline is what makes waiting sufficient.
   *
   * Rate-limited by the same clock as the chunk path, since committing restarts
   * it, so a high-volume catch-up gains no commits: they stay bounded by elapsed
   * time over the ceiling, not by frame count.
   */
  #armCatchUpFlush(): void {
    if (this.#catchUpFlushTimer != null || typeof window === "undefined") return;
    /* Nothing staged means nothing to show; committing anyway would re-run every
       projection to produce the array that is already there. */
    if (this.#frameBuffer.length === this.frames.length) return;
    const generation = this.#publishGeneration;
    const delay = Math.max(0, catchUpCeilingMs - (now() - this.#catchUpStartedAt));
    this.#catchUpFlushTimer = window.setTimeout(() => {
      this.#catchUpFlushTimer = null;
      /* Same guard as the queued per-paint flush: a session switch bumps the
         generation, and this buffer must not land in the new session's view. */
      if (generation !== this.#publishGeneration) return;
      this.#sinceCatchUpPublish = 0;
      this.#catchUpStartedAt = now();
      this.#publishFrames();
    }, delay);
  }

  /**
   * Commit on the next frame the browser paints, coalescing whatever arrives in
   * between. A burst of thirty events becomes one commit rather than thirty.
   *
   * While catching up, commit on the chunk schedule instead. One commit per paint
   * is the right rate for tailing a live run and far too many for a backlog of a
   * thousand events, each commit re-running every projection over a longer
   * timeline than the last.
   *
   * #hydrateCachedFrames drives its own loop and never reaches here, so this is
   * the catch-up path for history arriving over the stream — where there is no
   * loop to hang the schedule on, only frames landing one at a time.
   */
  #schedulePublish(): void {
    if (this.#catchingUp) {
      this.#sinceCatchUpPublish += 1;
      if (
        this.#sinceCatchUpPublish < framePublishChunkSize ||
        !publishAtChunkBoundary(true, now() - this.#catchUpStartedAt)
      ) {
        this.#armCatchUpFlush();
        return;
      }
      this.#sinceCatchUpPublish = 0;
      /* Restart the clock so the ceiling rate-limits rather than merely delays:
         without it every chunk past the first second commits, and chunks can
         pass far faster than the page can paint. */
      this.#catchUpStartedAt = now();
      this.#publishFrames();
      return;
    }
    if (this.#flushQueued) return;
    this.#flushQueued = true;
    const generation = this.#publishGeneration;
    const flush = () => {
      this.#flushQueued = false;
      /* Switching sessions bumps the generation, so a flush queued against the
         old one must not resurrect its frames into the new session's view. */
      if (generation !== this.#publishGeneration) return;
      this.#publishFrames();
    };
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(flush);
      return;
    }
    setTimeout(flush, 0);
  }

  /**
   * Take one frame off the stream: stage it, then commit on whichever schedule
   * suits what the server says this frame is.
   *
   * The server marks an event replay when it was already durable as the stream
   * opened. That is the only way to know a cold load is being caught up on
   * history: a client with no cache cannot tell a thousand backlogged events from
   * a thousand arriving live, and hydrating one commit at a time is what made a
   * fresh tab crawl. The absence of the mark means live, so an older server just
   * gets today's per-paint behavior.
   *
   * A subagent's own attach (#attachWorkflow) feeds this same pipeline with its
   * own seam, so the mark is only ordered per attach and not across them. The
   * mode therefore latches rather than tracking it per frame — see
   * catchingUpAfterFrame().
   */
  #appendFrame(
    frame: AgentSseFrame,
    options: { persist?: boolean; sourceWorkflowId?: string } = {}
  ): void {
    const isReplay = "replay" in frame.data && frame.data.replay === true;
    if (!isReplay) this.#liveFrameSeen = true;
    const catchingUp = catchingUpAfterFrame(isReplay, this.#liveFrameSeen);
    if (catchingUp !== this.#catchingUp) {
      this.#catchingUp = catchingUp;
      this.#catchUpStartedAt = now();
      this.#sinceCatchUpPublish = 0;
      /* Crossing to live commits the tail of the backlog immediately rather than
         holding it for a chunk that may never fill — a run that goes quiet right
         after catching up would otherwise leave its last events unpublished. */
      if (!catchingUp) this.#publishFrames();
    }
    this.#ingestFrame(frame, options);
    this.#schedulePublish();
  }

  #publisherWorkflowId(frame: AgentSseFrame): string | undefined {
    const sessionWorkflowId = this.session?.workflow_id;
    if (!("agent_id" in frame.data)) return sessionWorkflowId;
    const agentId = frame.data.agent_id;
    return (
      this.observedSubagents.find((agent) => agent.subagentId === agentId)
        ?.workflowId ?? sessionWorkflowId
    );
  }

  #handleSubagentEvent(frame: AgentSseFrame, parentWorkflowId = this.session?.workflow_id): void {
    if (!("type" in frame.data)) return;

    if (frame.event === "subagent_started") {
      this.#upsertSubagent(frame.data, parentWorkflowId);
      void this.#fetchAgentInterface(frame.data.workflow_id);
      void this.#fetchOperatorInterface(frame.data.workflow_id);
      return;
    }

    if (frame.event === "subagent_message_sent") {
      this.#upsertSubagent(
        { ...frame.data, targetTurn: frame.data.subagent_turn },
        parentWorkflowId
      );
      void this.#fetchAgentInterface(frame.data.workflow_id);
      void this.#fetchOperatorInterface(frame.data.workflow_id);
      return;
    }

    if (frame.event === "subagent_reply_received") {
      this.#upsertSubagent(
        { ...frame.data, targetTurn: frame.data.subagent_turn },
        parentWorkflowId
      );
      void this.#fetchAgentInterface(frame.data.workflow_id);
      void this.#fetchOperatorInterface(frame.data.workflow_id);
      return;
    }

    if (frame.event === "subagent_stopped") {
      this.#markWorkflowClosed(frame.data.workflow_id);
      this.#upsertSubagent({ ...frame.data, stopped: true }, parentWorkflowId);
      return;
    }

    if (frame.event === "subagent_stream_unavailable") {
      this.#upsertSubagent(frame.data, parentWorkflowId);
      void this.#resolveUnreadableSubagent(frame.data.workflow_id);
    }
  }

  /**
   * Ask Temporal what became of a child whose stream could not be read.
   *
   * Without this an operator's `/stop` on a subagent renders as still running
   * for anyone who did not watch it happen. The stop completes the child
   * workflow, and a completed workflow's stream cannot be mounted at all, so
   * the merge gives up and sends this marker — while the two events that DO say
   * "closed" both miss: `subagent_stopped` only fires when the parent stopped
   * the child, and the `operator_command_completed` carrying the stop is on the
   * child's own stream, which by then does not exist. A tab that saw the stop
   * live recovers from its frame cache; a second tab, or a cold load off the
   * session list, has nothing to recover from.
   *
   * Asking rather than assuming, because an unreadable stream is not proof of a
   * closed workflow — history aged out or a worker down produces this same
   * marker over a child that is still running. #applyWorkflowExecutionState
   * closes it only if the answer says closed, and a query that fails leaves the
   * child exactly as the marker found it.
   */
  async #resolveUnreadableSubagent(workflowId: string): Promise<void> {
    if (this.#isWorkflowClosed(workflowId)) return;
    try {
      await this.#refreshWorkflowExecutionState(workflowId);
    } catch {
      // Status is auxiliary here: the child stays as it was until something answers.
    }
  }

  goTo(index: number): void {
    this.viewIndex = Math.max(0, Math.min(index, this.total));
    this.following = this.viewIndex === this.total;
    if (this.following) this.pause();
  }

  stepBack(): void {
    this.pause();
    this.goTo(this.viewIndex - 1);
  }

  stepForward(): void {
    this.goTo(this.viewIndex + 1);
  }

  previousTurn(): void {
    this.pause();
    const target = [...this.turnMarkers]
      .reverse()
      .find((marker) => marker.index < this.viewIndex - 1);
    this.goTo(target?.index ?? 0);
  }

  nextTurn(): void {
    this.pause();
    const target = this.turnMarkers.find((marker) => marker.index >= this.viewIndex);
    this.goTo(target?.index ?? this.total);
  }

  jumpToLive(): void {
    this.goTo(this.total);
    this.following = true;
  }

  setPlaybackSpeed(speed: PlaybackSpeed): void {
    this.playbackSpeed = speed;
    if (this.playing) this.#restartTimer();
  }

  play(): void {
    if (this.playing) return;
    if (this.viewIndex >= this.total) this.goTo(0);
    this.playing = true;
    this.#restartTimer();
  }

  #restartTimer(): void {
    if (this.#timer != null) window.clearInterval(this.#timer);
    this.#timer = window.setInterval(() => {
      if (this.viewIndex >= this.total) {
        this.pause();
        this.following = true;
        return;
      }
      this.stepForward();
    }, basePlaybackDelayMs / this.playbackSpeed);
  }

  pause(): void {
    this.playing = false;
    if (this.#timer != null) {
      window.clearInterval(this.#timer);
      this.#timer = null;
    }
  }

  reset(): void {
    this.pause();
    this.goTo(0);
  }
}

export function createAgentRunController(): AgentRunController {
  return new AgentRunController();
}
