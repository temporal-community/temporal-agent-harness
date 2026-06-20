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
  buildMountedAgentGraph,
  type MountedAgentGraphInput
} from "./flowProjection";
import { buildReplayLog, buildReplayMarkers } from "./replayLog";
import { buildStepTimeline } from "./stepTimeline";
import { buildTranscript } from "./transcript";

export type PlaybackSpeed = 1 | 2 | 5 | 10;

export interface RunInfo {
  sessionId: string;
  agentLabel: string;
  models: string[];
  startedAt: number;
}

export interface MountedAgentStream {
  workflowId: string;
  role: "subagent";
  parentWorkflowId: string;
  handle: string;
  agentKey: string;
  label: string;
  frames: AgentSseFrame[];
  agentInterface?: AgentInterfaceFunction[];
  lastOffset: number;
  targetTurn: number | null;
  stopped: boolean;
}

type ReplayTimelineRole = "parent" | "subagent";

interface ReplayTimelineEntry {
  workflowId: string;
  role: ReplayTimelineRole;
  frame: AgentSseFrame;
}

const basePlaybackDelayMs = 700;
const activeSessionStorageKey = "temporal-agent-ui.active-session.v1";
const subagentStoragePrefix = "temporal-agent-ui.subagents.v1:";

interface StoredSubagentStreams {
  agents: MountedAgentStream[];
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

function subagentStorageKey(parentWorkflowId: string): string {
  return `${subagentStoragePrefix}${parentWorkflowId}`;
}

function readStoredSubagents(parentWorkflowId: string): MountedAgentStream[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(subagentStorageKey(parentWorkflowId));
    if (!raw) return [];
    const stored = JSON.parse(raw) as Partial<StoredSubagentStreams>;
    if (!Array.isArray(stored.agents)) return [];
    return stored.agents.filter(
      (agent): agent is MountedAgentStream =>
        agent?.role === "subagent" &&
        typeof agent.workflowId === "string" &&
        typeof agent.parentWorkflowId === "string" &&
        typeof agent.handle === "string" &&
        typeof agent.agentKey === "string" &&
        typeof agent.label === "string" &&
        Array.isArray(agent.frames) &&
        (!("agentInterface" in agent) || Array.isArray(agent.agentInterface)) &&
        typeof agent.lastOffset === "number"
    );
  } catch {
    return [];
  }
}

function writeStoredSubagents(parentWorkflowId: string, agents: MountedAgentStream[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      subagentStorageKey(parentWorkflowId),
      JSON.stringify({ agents } satisfies StoredSubagentStreams)
    );
  } catch {
    // Best-effort cache only; the live stream/history paths remain authoritative.
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

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timeout = window.setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeout);
        resolve();
      },
      { once: true }
    );
  });
}

export class MockRunController {
  #api: AgentApi;
  #initialized = false;

  frames = $state<AgentSseFrame[]>([]);
  mountedAgents = $state<MountedAgentStream[]>([]);
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
  lastOffset = $state(0);
  #streamVersion = 0;
  #connectionVersion = 0;
  #sendVersion = 0;
  #streamAbort: AbortController | null = null;
  #subagentAborts = new Map<string, AbortController>();
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
  graph = $derived(buildMountedAgentGraph(this.graphAgents));
  replayLog = $derived(buildReplayLog(this.visibleReplayFrames));
  fullReplayLog = $derived(buildReplayLog(this.allReplayFrames));
  supportTranscript = $derived(buildTranscript(this.frames));
  currentLogRow = $derived(
    this.fullReplayLog.rows.find((row) => row.index === this.viewIndex) ?? null
  );
  usage = $derived(summarizeCost(this.visibleReplayFrames));
  usageTimeline = $derived(buildUsageTimeline(this.allReplayFrames));
  stepTimeline = $derived(buildStepTimeline(this.allReplayFrames));
  anomalyMarkers = $derived(buildReplayMarkers(this.allReplayFrames));
  turnMarkers = $derived(
    this.allReplayFrames
      .map((frame, index) =>
        frame.event === "turn_started" && "type" in frame.data
          ? { index, turnNumber: frame.data.turn_number }
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
    const mountedByWorkflowId = new Map(
      this.mountedAgents.map((agent) => [agent.workflowId, agent])
    );
    const timeline: ReplayTimelineEntry[] = [];

    for (const frame of this.frames) {
      timeline.push({
        workflowId: session.workflow_id,
        role: "parent",
        frame
      });
      if (frame.event !== "subagent_message_sent" || !("type" in frame.data)) {
        continue;
      }
      const mounted = mountedByWorkflowId.get(frame.data.workflow_id);
      if (!mounted) continue;
      for (const childFrame of mounted.frames) {
        if (
          "type" in childFrame.data &&
          childFrame.data.turn_number === frame.data.subagent_turn
        ) {
          timeline.push({
            workflowId: mounted.workflowId,
            role: "subagent",
            frame: childFrame
          });
        }
      }
    }

    return timeline;
  }

  #graphAgents(): MountedAgentGraphInput[] {
    const session = this.session;
    if (!session) return [];
    const visibleSubagentWorkflowIds = new Set<string>();
    const visibleSubagentFrames = new Map<string, AgentSseFrame[]>();
    for (const frame of this.visibleFrames) {
      if (!("type" in frame.data)) continue;
      if (
        frame.event !== "subagent_started" &&
        frame.event !== "subagent_message_sent" &&
        frame.event !== "subagent_stopped"
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
    for (const agent of this.mountedAgents) {
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
      ...this.mountedAgents
        .filter((agent) => visibleSubagentWorkflowIds.has(agent.workflowId))
        .map((agent) => ({
          workflowId: agent.workflowId,
          role: agent.role,
          label: agent.label,
          parentWorkflowId: agent.parentWorkflowId,
          handle: agent.handle,
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

  #stopSubagentStreams(): void {
    for (const controller of this.#subagentAborts.values()) {
      controller.abort();
    }
    this.#subagentAborts.clear();
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

  #subagentLabel(agentKey: string, handle: string): string {
    const descriptor = this.agents.find((agent) => agent.key === agentKey);
    return `${descriptor?.label ?? agentKey} (${handle})`;
  }

  #upsertSubagent(data: {
    workflow_id: string;
    handle: string;
    agent_key: string;
    targetTurn?: number;
    stopped?: boolean;
  }, parentWorkflowId = this.session?.workflow_id): void {
    if (!parentWorkflowId) return;
    const existing = this.mountedAgents.find(
      (agent) => agent.workflowId === data.workflow_id
    );
    const next: MountedAgentStream = {
      workflowId: data.workflow_id,
      role: "subagent",
      parentWorkflowId,
      handle: data.handle,
      agentKey: data.agent_key,
      label: this.#subagentLabel(data.agent_key, data.handle),
      frames: existing?.frames ?? [],
      agentInterface:
        this.agentInterfaces[data.workflow_id] ?? existing?.agentInterface,
      lastOffset: existing?.lastOffset ?? 0,
      targetTurn:
        data.targetTurn == null
          ? existing?.targetTurn ?? null
          : Math.max(existing?.targetTurn ?? 0, data.targetTurn),
      stopped: data.stopped ?? existing?.stopped ?? false
    };
    this.mountedAgents = [
      ...this.mountedAgents.filter((agent) => agent.workflowId !== data.workflow_id),
      next
    ];
    this.#persistSubagentCache();
  }

  #loadSubagentCache(parentWorkflowId: string): void {
    this.mountedAgents = readStoredSubagents(parentWorkflowId);
    const cachedInterfaces = Object.fromEntries(
      this.mountedAgents
        .filter((agent) => agent.agentInterface?.length)
        .map((agent) => [agent.workflowId, agent.agentInterface ?? []])
    );
    this.agentInterfaces = { ...this.agentInterfaces, ...cachedInterfaces };
  }

  #persistSubagentCache(): void {
    const parentWorkflowId = this.session?.workflow_id;
    if (!parentWorkflowId) return;
    writeStoredSubagents(parentWorkflowId, this.mountedAgents);
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
      if (this.mountedAgents.some((agent) => agent.workflowId === workflowId)) {
        this.mountedAgents = this.mountedAgents.map((agent) =>
          agent.workflowId === workflowId ? { ...agent, agentInterface } : agent
        );
        this.#persistSubagentCache();
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
      const supportAgent = agents.find((agent) => agent.key === "qa") ?? agents[0];
      if (!supportAgent) throw new Error("No support agent is registered.");

      const sessions = await this.#api.listSessions();
      this.sessions = sessions;
      const storedSessionId = readStoredActiveSessionId();
      const storedSession = storedSessionId
        ? sessions.find((item) => item.workflow_id === storedSessionId)
        : null;
      const existing = [...sessions]
        .reverse()
        .find((item) => item.agent_workflow_type === supportAgent.workflow_type);

      if (storedSession) {
        this.session = storedSession;
      } else if (existing) {
        this.session = existing;
      } else {
        this.session = await this.#api.createSession({
          agent_workflow_type: supportAgent.workflow_type,
          is_message_queuing_enabled: true
        });
        this.sessions = [...this.sessions, this.session];
      }
      writeStoredActiveSessionId(this.session.workflow_id);
      this.#loadSubagentCache(this.session.workflow_id);
      void this.#fetchAgentInterface(this.session.workflow_id);

      if (!this.#isCurrentConnection(connectionVersion)) return;
      await this.attach(0);
    } catch (error) {
      if (this.#isCurrentConnection(connectionVersion) && !isAbortError(error)) {
        this.connectionError =
          error instanceof Error ? error.message : "Failed to initialize support session.";
      }
    } finally {
      if (this.#isCurrentConnection(connectionVersion)) this.connecting = false;
    }
  }

  async startNewSession(workflowType?: string): Promise<void> {
    const connectionVersion = this.#beginConnection();
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

      if (!agent) throw new Error("No support agent is registered.");

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
      this.#loadSubagentCache(session.workflow_id);
      void this.#fetchAgentInterface(session.workflow_id);
      await this.attach(0);
    } catch (error) {
      if (this.#isCurrentConnection(connectionVersion) && !isAbortError(error)) {
        this.connectionError =
          error instanceof Error ? error.message : "Failed to create support session.";
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
    this.#loadSubagentCache(session.workflow_id);
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

  async attach(fromOffset = this.lastOffset): Promise<void> {
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

  #subagentTurnEnded(workflowId: string, turnNumber: number): boolean {
    const mounted = this.mountedAgents.find((agent) => agent.workflowId === workflowId);
    return (
      mounted?.frames.some(
        (frame) =>
          frame.event === "turn_end" &&
          "type" in frame.data &&
          frame.data.turn_number >= turnNumber
      ) ?? false
    );
  }

  async #fetchSubagentMessages(workflowId: string, replace = false): Promise<void> {
    const mounted = this.mountedAgents.find((agent) => agent.workflowId === workflowId);
    if (!mounted || mounted.stopped) return;
    const existing = this.#subagentAborts.get(workflowId);
    if (existing) {
      if (!replace) return;
      existing.abort();
      this.#subagentAborts.delete(workflowId);
    }

    const parentWorkflowId = mounted.parentWorkflowId;
    const controller = new AbortController();
    this.#subagentAborts.set(workflowId, controller);
    try {
      while (!controller.signal.aborted) {
        const current = this.mountedAgents.find((agent) => agent.workflowId === workflowId);
        if (
          !current ||
          current.stopped ||
          this.session?.workflow_id !== parentWorkflowId ||
          (current.targetTurn != null && this.#subagentTurnEnded(workflowId, current.targetTurn))
        ) {
          break;
        }

        let sawFrame = false;
        for await (const frame of this.#api.streamHistory(
          workflowId,
          current.lastOffset,
          controller.signal
        )) {
          if (
            controller.signal.aborted ||
            this.session?.workflow_id !== parentWorkflowId ||
            !this.mountedAgents.some((agent) => agent.workflowId === workflowId)
          ) {
            break;
          }
          sawFrame = true;
          this.#appendSubagentFrame(workflowId, frame);
        }

        const latest = this.mountedAgents.find((agent) => agent.workflowId === workflowId);
        if (
          !latest ||
          latest.stopped ||
          latest.targetTurn == null ||
          this.#subagentTurnEnded(workflowId, latest.targetTurn)
        ) {
          break;
        }

        await delay(sawFrame ? 50 : 250, controller.signal);
      }
    } catch (error) {
      if (!isAbortError(error) && this.session?.workflow_id === parentWorkflowId) {
        this.connectionError =
          error instanceof Error ? error.message : "Failed to fetch subagent stream.";
      }
    } finally {
      if (this.#subagentAborts.get(workflowId) === controller) {
        this.#subagentAborts.delete(workflowId);
      }
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
        expected_turn: expectedTurn,
        from_offset: this.lastOffset
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
      await this.attach(this.lastOffset);
    } finally {
      if (sendVersion === this.#sendVersion) this.sending = false;
      this.#finishStream(controller);
    }
  }

  async approveTool(toolId: string, approved: boolean): Promise<void> {
    const session = this.session;
    if (!session) throw new Error("No active session.");

    this.connectionError = null;
    try {
      await this.#api.approve({
        session_id: session.workflow_id,
        tool_id: toolId,
        approved,
        reason: approved ? null : "Rejected in chat.",
        remember: false
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
    this.#stopSubagentStreams();
    this.frames = [];
    this.mountedAgents = [];
    this.viewIndex = 0;
    this.following = false;
    this.expectedTurn = 1;
    this.lastOffset = 0;
  }

  #appendFrame(frame: AgentSseFrame): void {
    if (!("type" in frame.data)) {
      this.connectionError = frame.data.message;
    }

    if (
      "offset" in frame.data &&
      this.frames.some((item) => "offset" in item.data && item.data.offset === frame.data.offset)
    ) {
      return;
    }

    this.frames = [...this.frames, frame];
    this.following = true;
    this.viewIndex = this.total;

    if ("offset" in frame.data && typeof frame.data.offset === "number") {
      this.lastOffset = Math.max(this.lastOffset, frame.data.offset);
    }
    if ("type" in frame.data && frame.data.turn_number >= this.expectedTurn) {
      this.expectedTurn = frame.data.turn_number + 1;
    }
    if (frame.event === "turn_started" && frame.data.turn_number === 1) {
      this.#recordInitialUserMessage(renderUserMessage(frame.data.user_message));
    }
    this.#handleSubagentEvent(frame);
  }

  #appendSubagentFrame(workflowId: string, frame: AgentSseFrame): void {
    this.mountedAgents = this.mountedAgents.map((agent) => {
      if (agent.workflowId !== workflowId) return agent;
      if (
        "offset" in frame.data &&
        agent.frames.some(
          (item) => "offset" in item.data && item.data.offset === frame.data.offset
        )
      ) {
        return agent;
      }
      const lastOffset =
        "offset" in frame.data && typeof frame.data.offset === "number"
          ? Math.max(agent.lastOffset, frame.data.offset)
          : agent.lastOffset;
      return {
        ...agent,
        frames: [...agent.frames, frame],
        lastOffset
      };
    });
    this.#persistSubagentCache();
    this.#handleSubagentEvent(frame, workflowId);
    if (this.following) this.viewIndex = this.total;
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
      void this.#fetchSubagentMessages(frame.data.workflow_id, true);
      return;
    }

    if (frame.event === "subagent_stopped") {
      this.#upsertSubagent({ ...frame.data, stopped: true }, parentWorkflowId);
      this.#subagentAborts.get(frame.data.workflow_id)?.abort();
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
    this.goTo(target?.index ?? this.frames.length);
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

export function createMockRunController(): MockRunController {
  return new MockRunController();
}
