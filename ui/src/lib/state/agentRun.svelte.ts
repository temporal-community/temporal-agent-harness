import type {
  AgentInboundMessage,
  AgentInterfaceFunction,
  AgentSseFrame
} from "$lib/api/types";
import type { AgentApi } from "$lib/api/client";
import type { AgentDescriptor, Session } from "$lib/api/types";
import { HttpAgentApi } from "$lib/api/httpClient";
import { realisticQaScenario } from "$lib/mock/scenarios";
import { buildUsageTimeline, summarizeCost } from "$lib/cost/pricing";
import {
  buildAgentTreeGraph,
  type AgentGraphSource
} from "./flowProjection";
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
  targetTurn: number | null;
  stopped: boolean;
}

type ReplayTimelineRole = "parent" | "subagent";

interface ReplayTimelineEntry extends StepTimelineFrame {
  workflowId: string;
  role: ReplayTimelineRole;
  frame: AgentSseFrame;
}

const basePlaybackDelayMs = 700;
const activeSessionStorageKey = "temporal-agent-ui.active-session.v1";

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
    if (message.type !== "slash_command" || !message.payload?.name) return value;
    return `/${message.payload.name}${message.payload.arg ? ` ${message.payload.arg}` : ""}`;
  } catch {
    return value;
  }
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

export class AgentRunController {
  #api: AgentApi;
  #initialized = false;

  frames = $state<AgentSseFrame[]>([]);
  observedSubagents = $state<ObservedSubagent[]>([]);
  agentInterfaces = $state<Record<string, AgentInterfaceFunction[]>>({});
  viewIndex = $state(0);
  playing = $state(false);
  following = $state(false);
  connecting = $state(false);
  sending = $state(false);
  creatingSession = $state(false);
  connectionError = $state<string | null>(null);
  playbackSpeed = $state<PlaybackSpeed>(1);
  agents = $state<AgentDescriptor[]>([]);
  sessions = $state<Session[]>([]);
  session = $state<Session | null>(null);
  expectedTurn = $state(1);
  lastResumeOffset = $state(0);
  #streamVersion = 0;
  #connectionVersion = 0;
  #sendVersion = 0;
  #streamAbort: AbortController | null = null;
  #interfaceRequests = new Set<string>();
  #timer: number | null = null;

  constructor(api: AgentApi = new HttpAgentApi()) {
    this.#api = api;
  }

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
  replayLog = $derived(buildReplayLog(this.visibleReplayTimeline));
  fullReplayLog = $derived(buildReplayLog(this.replayTimeline));
  chatTranscript = $derived(
    buildTranscript(
      this.replayTimeline
        .filter((entry) => entry.role === "parent")
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
    return this.replayTimeline.length;
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
          stopped: agent.stopped
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
    return {
      controller,
      signal: controller.signal,
      streamVersion: ++this.#streamVersion
    };
  }

  #finishStream(controller: AbortController): void {
    if (this.#streamAbort === controller) this.#streamAbort = null;
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
      const storedSessionId = readStoredActiveSessionId();
      const storedSession = storedSessionId
        ? sessions.find((item) => item.workflow_id === storedSessionId)
        : null;
      const existing = [...sessions]
        .reverse()
        .find((item) => item.agent_workflow_type === defaultAgent.workflow_type);

      if (storedSession) {
        this.session = storedSession;
      } else if (existing) {
        this.session = existing;
      } else {
        this.session = await this.#api.createSession({
          agent_workflow_type: defaultAgent.workflow_type,
          is_message_queuing_enabled: true
        });
        this.sessions = [...this.sessions, this.session];
      }
      writeStoredActiveSessionId(this.session.workflow_id);
      void this.#fetchAgentInterface(this.session.workflow_id);

      if (!this.#isCurrentConnection(connectionVersion)) return;
      await this.attach(0);
    } catch (error) {
      if (this.#isCurrentConnection(connectionVersion) && !isAbortError(error)) {
        this.connectionError =
          error instanceof Error ? error.message : "Failed to initialize agent session.";
      }
    } finally {
      if (this.#isCurrentConnection(connectionVersion)) this.connecting = false;
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

    try {
      await this.attach(0);
    } catch (error) {
      if (this.#isCurrentConnection(connectionVersion) && !isAbortError(error)) {
        this.connectionError =
          error instanceof Error ? error.message : "Failed to load selected session.";
      }
    } finally {
      if (this.#isCurrentConnection(connectionVersion)) this.connecting = false;
    }
  }

  async attach(fromOffset = this.lastResumeOffset): Promise<void> {
    const session = this.session;
    if (!session) return;

    const { controller, signal, streamVersion } = this.#beginStream();
    try {
      for await (const frame of this.#api.attach(session.workflow_id, fromOffset, signal)) {
        if (streamVersion !== this.#streamVersion || this.session?.workflow_id !== session.workflow_id) {
          break;
        }
        this.#appendFrame(frame);
      }
    } catch (error) {
      if (!isAbortError(error)) throw error;
    } finally {
      this.#finishStream(controller);
    }
  }

  async sendMessage(message: string): Promise<void> {
    if (!message.trim()) return;
    await this.initialize();
    const session = this.session;
    if (!session) return;

    this.pause();
    const expectedTurn = this.expectedTurn;
    this.expectedTurn += 1;
    const sendVersion = ++this.#sendVersion;
    this.sending = true;
    this.connectionError = null;
    const { controller, signal, streamVersion } = this.#beginStream();

    try {
      this.#recordInitialUserMessage(message);
      for await (const frame of this.#api.chat({
        session_id: session.workflow_id,
        message: this.#messageForSession(message, session),
        expected_turn: expectedTurn
      }, signal)) {
        if (streamVersion !== this.#streamVersion || this.session?.workflow_id !== session.workflow_id) {
          break;
        }
        this.#appendFrame(frame);
      }
    } catch (error) {
      if (
        isAbortError(error) ||
        streamVersion !== this.#streamVersion ||
        this.session?.workflow_id !== session.workflow_id
      ) {
        return;
      }
      this.expectedTurn = Math.max(1, expectedTurn);
      this.connectionError =
        error instanceof Error ? error.message : "Failed to send message.";
      await this.attach(this.lastResumeOffset);
    } finally {
      if (sendVersion === this.#sendVersion) this.sending = false;
      this.#finishStream(controller);
    }
  }

  async approveTool(
    toolId: string,
    approved: boolean,
    remember = false
  ): Promise<void> {
    const session = this.session;
    if (!session) throw new Error("No active session.");

    this.connectionError = null;
    try {
      await this.#api.approve({
        session_id: session.workflow_id,
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

  #messageForSession(message: string, session: Session): AgentInboundMessage {
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

  #resetSessionView(): void {
    this.pause();
    this.#stopStream();
    this.frames = [];
    this.observedSubagents = [];
    this.viewIndex = 0;
    this.following = false;
    this.expectedTurn = 1;
    this.lastResumeOffset = 0;
  }

  #appendFrame(frame: AgentSseFrame): void {
    if (!("type" in frame.data)) {
      this.connectionError = frame.data.message;
    }
    const publisherWorkflowId = this.#publisherWorkflowId(frame);
    const isRootFrame = publisherWorkflowId === this.session?.workflow_id;

    this.frames = [...this.frames, frame];
    this.following = true;
    this.viewIndex = this.total;

    if (
      "resume_offset" in frame.data &&
      typeof frame.data.resume_offset === "number"
    ) {
      this.lastResumeOffset = Math.max(
        this.lastResumeOffset,
        frame.data.resume_offset
      );
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
    this.#handleSubagentEvent(frame, publisherWorkflowId);
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
      return;
    }

    if (frame.event === "subagent_message_sent") {
      this.#upsertSubagent(
        { ...frame.data, targetTurn: frame.data.subagent_turn },
        parentWorkflowId
      );
      void this.#fetchAgentInterface(frame.data.workflow_id);
      return;
    }

    if (frame.event === "subagent_reply_received") {
      this.#upsertSubagent(
        { ...frame.data, targetTurn: frame.data.subagent_turn },
        parentWorkflowId
      );
      void this.#fetchAgentInterface(frame.data.workflow_id);
      return;
    }

    if (frame.event === "subagent_stopped") {
      this.#upsertSubagent({ ...frame.data, stopped: true }, parentWorkflowId);
      return;
    }

    if (frame.event === "subagent_stream_unavailable") {
      this.#upsertSubagent(frame.data, parentWorkflowId);
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

  nextMarker(): void {
    this.pause();
    const target = this.anomalyMarkers.find((marker) => marker.index > this.viewIndex);
    if (target) this.goTo(target.index);
  }

  previousMarker(): void {
    this.pause();
    const target = [...this.anomalyMarkers]
      .reverse()
      .find((marker) => marker.index < this.viewIndex);
    if (target) this.goTo(target.index);
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
