// Folds a session's frames, one at a time, into messages and observable state for every agent
// in the tree, and records which of them each frame changed. The merged stream carries the
// root agent's events and, recursively, every subagent's, each stamped with its tree-unique
// `agent_id`; every agent gets its own AgentProjection, and SessionProjection routes frames to
// them and links each parent's subagent parts to the child's messages.
//
// Every frame touches one message (or one state document) found by id, so applying N frames
// costs O(N) however long the session is. A part that changes is replaced, never edited in
// place, so a message handed to a consumer never changes under it.

import type {
  AgentEventFrame,
  AgentSseFrame,
  JsonRecord,
  JsonValue
} from "./frames.ts";
import { applyOps } from "./jsonPatch.ts";
import type { HarnessMessage, MessagePart, ToolPart } from "./messages.ts";

type Message = HarnessMessage;

export interface StateDocument {
  value: JsonValue;
  version: number;
  /** Why later patches are not being applied, if they are not: a failed op, or a version gap
   *  (the stream was attached after the snapshot). The value stops at the last good version. */
  error: string | null;
}

/** What is known about an agent in the session's tree, apart from its messages and state. */
export interface AgentInfo {
  agentId: string;
  /** The agent that started this one; null for the root, or a subagent seen before its parent. */
  parentId: string | null;
  /** Which of its parent's wired agent types it is; null for the root. */
  agentKey: string | null;
  /** Its own workflow id; null for the root, whose workflow id is the session id. */
  workflowId: string | null;
  /** `stopped` once its parent stopped it: nothing more will arrive from it. */
  lifecycle: "running" | "stopped";
  /** Why its own events could not be read, if they could not. Its parent's view is unaffected. */
  unavailable: string | null;
}

/** One agent's changes since the last flush. `info` is present when it changed, and always the
 *  first time the agent appears. */
export interface AgentChanges {
  info?: AgentInfo;
  /** Each changed message as a fresh object, with its index. An index equal to the previous
   *  length is an append. */
  messages?: Array<[index: number, message: Message]>;
  states?: Array<[stateId: string, document: StateDocument]>;
  agentStatus?: "idle" | "busy";
}

export interface ProjectionChanges {
  agents: Array<[agentId: string, changes: AgentChanges]>;
  /** The whole `pending` list, when it changed. */
  pending: readonly Message[] | null;
}

interface PartLocation {
  messageId: string;
  index: number;
}

/** One agent's messages and state, folded from its own frames. */
export class AgentProjection {
  readonly messages: Message[] = [];
  readonly states = new Map<string, StateDocument>();
  agentStatus: "idle" | "busy" = "idle";
  /** The highest turn number this agent has been seen to open. */
  lastTurnNumber = 0;
  info: AgentInfo;

  readonly #indexById = new Map<string, number>();
  readonly #tools = new Map<string, PartLocation>();
  readonly #subagentTurns = new Map<string, PartLocation>();
  readonly #touchedMessages = new Set<string>();
  readonly #touchedStates = new Set<string>();
  #statusTouched = false;
  #infoTouched = true;

  constructor(info: AgentInfo) {
    this.info = info;
  }

  has(messageId: string): boolean {
    return this.#indexById.has(messageId);
  }

  hasTool(toolId: string): boolean {
    return this.#tools.has(toolId);
  }

  setInfo(fields: Partial<AgentInfo>): void {
    this.info = { ...this.info, ...fields };
    this.#infoTouched = true;
  }

  /** Add a message the stream has not admitted yet (a send whose submit just returned). */
  addMessage(message: Message): void {
    this.#push(message);
  }

  /** Tool calls waiting on a client, in the order they started waiting. */
  awaitingClient(): ToolPart[] {
    const parts: ToolPart[] = [];
    for (const { messageId, index } of this.#tools.values()) {
      const part = this.#message(messageId)?.parts[index];
      if (part?.type === "tool" && part.state === "awaiting_callback") parts.push(part);
    }
    return parts;
  }

  /** Point this agent's open turn with `subagentId` at the message the subagent ran for it. */
  linkSubagentMessage(subagentId: string, subagentTurn: number, messageId: string): void {
    const location = this.#subagentTurns.get(subagentId);
    if (!location) return;
    const part = this.#message(location.messageId)?.parts[location.index];
    if (part?.type !== "subagent" || part.subagentTurn !== subagentTurn || part.messageId !== null) return;
    this.#setPart(location.messageId, location.index, { ...part, messageId });
  }

  /** Close (or mark unavailable) this agent's open turn with `subagentId`. */
  closeSubagent(subagentId: string, status: "ok" | "error" | "unavailable"): void {
    const location = this.#subagentTurns.get(subagentId);
    if (!location) return;
    const part = this.#message(location.messageId)?.parts[location.index];
    if (part?.type !== "subagent" || (part.status !== "running" && status === "unavailable")) return;
    this.#setPart(location.messageId, location.index, { ...part, status });
    if (status !== "unavailable") this.#subagentTurns.delete(subagentId);
  }

  /** Everything changed since the last call, or null if nothing did. */
  takeChanges(): AgentChanges | null {
    const changes: AgentChanges = {};
    if (this.#infoTouched) changes.info = this.info;
    if (this.#touchedMessages.size > 0) {
      const messages: Array<[number, Message]> = [];
      for (const id of this.#touchedMessages) {
        const index = this.#indexById.get(id);
        if (index === undefined) continue;
        const message = this.messages[index]!;
        messages.push([index, { ...message, parts: [...message.parts] }]);
      }
      changes.messages = messages.sort((a, b) => a[0] - b[0]);
    }
    if (this.#touchedStates.size > 0) {
      changes.states = [...this.#touchedStates].flatMap((id) => {
        const doc = this.states.get(id);
        return doc ? [[id, { ...doc, value: clone(doc.value) }] as [string, StateDocument]] : [];
      });
    }
    if (this.#statusTouched) changes.agentStatus = this.agentStatus;
    this.#touchedMessages.clear();
    this.#touchedStates.clear();
    this.#statusTouched = false;
    this.#infoTouched = false;
    return Object.keys(changes).length > 0 ? changes : null;
  }

  apply(frame: AgentEventFrame): void {
    const data = frame.data;
    const id = data.message_id;
    switch (frame.event) {
      case "message_accepted": {
        const d = frame.data;
        const known = this.#message(d.message_id!);
        if (known) {
          this.#update(known.id, {
            disposition: d.disposition,
            turnNumber: d.turn_number,
            acceptedAt: d.timestamp
          });
        } else {
          this.#push({
            id: d.message_id!,
            handler: d.handler,
            input: d.payload,
            output: null,
            status: "accepted",
            disposition: d.disposition,
            turnNumber: d.turn_number,
            parts: [],
            error: null,
            acceptedAt: d.timestamp,
            startedAt: null,
            endedAt: null
          });
        }
        return;
      }
      case "message_handler_start":
        return this.#update(id, { status: "running", startedAt: data.timestamp });
      case "message_handler_end":
        return this.#update(id, {
          status: "done",
          output: frame.data.output,
          endedAt: data.timestamp
        });
      case "message_handler_error":
        return this.#update(id, {
          status: "error",
          error: frame.data.message,
          endedAt: data.timestamp
        });
      case "turn_started":
        this.lastTurnNumber = Math.max(this.lastTurnNumber, data.turn_number);
        return this.#setStatus("busy");
      case "turn_end":
        return this.#setStatus("idle");
      case "model_interaction_started":
        return this.#append(id, {
          type: "model_interaction",
          model: frame.data.model,
          status: "running",
          usage: null
        });
      case "model_interaction_ended": {
        const d = frame.data;
        return this.#replaceLast(id, "model_interaction", (p) =>
          p.status === "running"
            ? { ...p, status: "done", usage: d.usage, model: d.model ?? p.model }
            : null
        );
      }
      case "reply_delta":
        return this.#appendText(id, "reply_delta", frame.data.text);
      case "thought_summary":
        return this.#appendText(id, "thought_summary", thoughtDeltaText(frame.data.delta));
      case "text_annotation":
        for (const annotation of frame.data.delta.annotations ?? []) {
          this.#append(id, { type: "text_annotation", annotation });
        }
        return;
      case "tool_requested":
        return this.#tool(frame, (p) => ({ ...p, input: frame.data.tool_input }));
      case "tool_approval_requested":
        return this.#tool(frame, (p) => ({
          ...p,
          input: frame.data.tool_input,
          state: "awaiting_approval"
        }));
      case "auto_approval_evaluation_started": {
        const d = frame.data;
        return this.#tool(frame, (p) => ({
          ...p,
          state: "evaluating",
          evaluations: [
            ...p.evaluations,
            {
              id: d.evaluation_id,
              evaluator: d.evaluator,
              status: "running",
              verdict: null,
              reason: null,
              details: {},
              error: null
            }
          ]
        }));
      }
      case "auto_approval_evaluation_ended": {
        const d = frame.data;
        return this.#evaluation(frame, d.evaluation_id, {
          status: "ended",
          verdict: d.verdict,
          reason: d.reason,
          details: d.details
        });
      }
      case "auto_approval_evaluation_superseded":
        return this.#evaluation(frame, frame.data.evaluation_id, {
          status: "superseded",
          verdict: frame.data.verdict
        });
      case "auto_approval_evaluation_error":
        return this.#evaluation(frame, frame.data.evaluation_id, {
          status: "error",
          error: frame.data.message
        });
      case "tool_approval_resolved": {
        const d = frame.data;
        return this.#tool(frame, (p) => ({
          ...p,
          state: d.approved ? "approved" : "denied",
          approval: { approved: d.approved, reason: d.reason, remember: d.remember }
        }));
      }
      case "tool_start":
        return this.#tool(frame, (p) => ({ ...p, state: "running", input: frame.data.tool_input }));
      case "tool_progress_delta":
        return this.#tool(frame, (p) => ({ ...p, progress: frame.data.progress_delta }));
      case "callback_requested": {
        const d = frame.data;
        return this.#tool(frame, (p) => ({
          ...p,
          state: "awaiting_callback",
          input: d.tool_input,
          callback: { outputSchema: d.output_schema, outcome: null }
        }));
      }
      case "callback_resolved": {
        const d = frame.data;
        return this.#tool(frame, (p) => ({
          ...p,
          state: "running",
          callback: { outputSchema: p.callback?.outputSchema ?? {}, outcome: d.outcome },
          error: d.error
        }));
      }
      case "tool_end":
        return this.#tool(frame, (p) => ({ ...p, state: "done", output: frame.data.tool_output }));
      case "tool_error":
        return this.#tool(frame, (p) => ({ ...p, state: "failed", error: frame.data.message }));
      case "subagent_message_sent": {
        const d = frame.data;
        const message = this.#message(id);
        if (!message) return;
        this.#subagentTurns.set(d.subagent_id, { messageId: message.id, index: message.parts.length });
        return this.#append(id, {
          type: "subagent",
          subagentId: d.subagent_id,
          agentKey: d.agent_key,
          workflowId: d.workflow_id,
          handler: d.handler,
          subagentTurn: d.subagent_turn,
          status: "running",
          messageId: null
        });
      }
      case "subagent_reply_received":
        return this.closeSubagent(frame.data.subagent_id, frame.data.outcome);
      case "subagent_started":
      case "subagent_stopped":
      case "subagent_stream_unavailable":
        return;
      case "state_snapshot": {
        const d = frame.data;
        this.states.set(d.state_id, { value: clone(d.value as JsonValue), version: d.version, error: null });
        this.#touchedStates.add(d.state_id);
        return;
      }
      case "state_patch": {
        const d = frame.data;
        const doc = this.states.get(d.state_id);
        if (!doc) {
          this.states.set(d.state_id, {
            value: null,
            version: d.version,
            error: "no snapshot of this state was received, so its patches cannot be applied"
          });
        } else if (doc.error !== null || d.version <= doc.version) {
          return;
        } else if (d.version !== doc.version + 1) {
          doc.error = `patch ${d.version} arrived after ${doc.version}; versions in between are missing`;
        } else {
          const result = applyOps(doc.value, d.ops);
          doc.value = result.doc;
          doc.version = d.version;
          doc.error = result.error;
        }
        this.#touchedStates.add(d.state_id);
        return;
      }
      default:
        frame satisfies never;
    }
  }

  #message(id: string | null): Message | undefined {
    if (id === null) return undefined;
    const index = this.#indexById.get(id);
    return index === undefined ? undefined : this.messages[index];
  }

  #push(message: Message): void {
    this.#indexById.set(message.id, this.messages.length);
    this.messages.push(message);
    this.#touchedMessages.add(message.id);
  }

  #update(id: string | null, fields: Partial<Message>): void {
    if (id === null) return;
    const index = this.#indexById.get(id);
    if (index === undefined) return;
    this.messages[index] = { ...this.messages[index]!, ...fields } as Message;
    this.#touchedMessages.add(id);
  }

  #setPart(messageId: string, index: number, part: MessagePart): void {
    const message = this.#message(messageId);
    if (!message) return;
    message.parts[index] = part;
    this.#touchedMessages.add(messageId);
  }

  #append(id: string | null, part: MessagePart): void {
    const message = this.#message(id);
    if (!message) return;
    message.parts.push(part);
    this.#touchedMessages.add(message.id);
  }

  #appendText(id: string | null, type: "reply_delta" | "thought_summary", text: string): void {
    if (text === "") return;
    const message = this.#message(id);
    if (!message) return;
    const last = message.parts.at(-1);
    if (last?.type === type) this.#setPart(message.id, message.parts.length - 1, { type, text: last.text + text });
    else this.#append(message.id, { type, text });
  }

  #replaceLast<T extends MessagePart["type"]>(
    id: string | null,
    type: T,
    replace: (part: Extract<MessagePart, { type: T }>) => MessagePart | null
  ): void {
    const message = this.#message(id);
    if (!message) return;
    for (let i = message.parts.length - 1; i >= 0; i--) {
      const part = message.parts[i]!;
      if (part.type !== type) continue;
      const next = replace(part as Extract<MessagePart, { type: T }>);
      if (next) this.#setPart(message.id, i, next);
      return;
    }
  }

  /** Update the tool part a tool event is about, creating it on first sight. A built-in tool
   *  can start without a `tool_requested`, and a resolution published by a policy cascade
   *  carries no `message_id`, so the part is found by `tool_id`. */
  #tool(
    frame: Extract<AgentEventFrame, { data: { tool_id: string } }>,
    change: (part: ToolPart) => ToolPart
  ): void {
    const d = frame.data;
    let location = this.#tools.get(d.tool_id);
    if (!location) {
      const message = this.#message(d.message_id);
      if (!message) return;
      location = { messageId: message.id, index: message.parts.length };
      this.#tools.set(d.tool_id, location);
      message.parts.push({
        type: "tool",
        toolId: d.tool_id,
        toolName: d.tool_name,
        state: "requested",
        input: {},
        output: null,
        error: null,
        progress: null,
        approval: null,
        evaluations: [],
        callback: null
      });
    }
    const part = this.#message(location.messageId)?.parts[location.index];
    if (part?.type === "tool") this.#setPart(location.messageId, location.index, change(part));
  }

  #evaluation(
    frame: Extract<AgentEventFrame, { data: { tool_id: string } }>,
    evaluationId: string,
    fields: Partial<ToolPart["evaluations"][number]>
  ): void {
    this.#tool(frame, (p) => {
      const evaluations = p.evaluations.map((e) => (e.id === evaluationId ? { ...e, ...fields } : e));
      const deciding = evaluations.some((e) => e.status === "running");
      // An evaluator that ends without settling the gate (an escalate, a failure) leaves it
      // with a person; one that settles it is followed by the resolution itself.
      const state = p.state === "evaluating" && !deciding ? "awaiting_approval" : p.state;
      return { ...p, evaluations, state };
    });
  }

  #setStatus(status: "idle" | "busy"): void {
    this.agentStatus = status;
    this.#statusTouched = true;
  }
}

/** Every agent in a session's tree, and this client's sends that the root has not admitted. */
export class SessionProjection {
  /** The agent the session was opened for: the one whose events the merged stream starts with. */
  rootAgentId: string | null = null;
  readonly agents = new Map<string, AgentProjection>();
  /** Messages submitted from this client that the stream has not admitted yet. */
  pending: readonly Message[] = [];
  #pendingTouched = false;

  get root(): AgentProjection | undefined {
    return this.rootAgentId === null ? undefined : this.agents.get(this.rootAgentId);
  }

  apply(frame: AgentSseFrame): void {
    if (frame.event === "stream_error") return;
    const data = frame.data;
    if (frame.event === "subagent_stream_unavailable") {
      // Synthesized by the merge and stamped with the subagent's own id.
      const child = this.#agent(frame.data.subagent_id);
      child.setInfo({
        unavailable: frame.data.reason || "its events could not be read",
        workflowId: child.info.workflowId ?? frame.data.workflow_id
      });
      if (child.info.parentId !== null) {
        this.agents.get(child.info.parentId)?.closeSubagent(frame.data.subagent_id, "unavailable");
      }
      return;
    }

    this.rootAgentId ??= data.agent_id;
    const agent = this.#agent(data.agent_id);
    switch (frame.event) {
      case "subagent_started":
      case "subagent_message_sent":
      case "subagent_stopped": {
        const d = frame.data;
        const child = this.#agent(d.subagent_id);
        const known = { parentId: data.agent_id, agentKey: d.agent_key, workflowId: d.workflow_id };
        const stopped = frame.event === "subagent_stopped" ? { lifecycle: "stopped" as const } : {};
        if (
          child.info.parentId !== known.parentId ||
          child.info.agentKey !== known.agentKey ||
          child.info.workflowId !== known.workflowId ||
          (stopped.lifecycle && child.info.lifecycle !== "stopped")
        ) {
          child.setInfo({ ...known, ...stopped });
        }
        break;
      }
    }
    agent.apply(frame);

    if (frame.event === "message_accepted" && frame.data.message_id !== null) {
      const messageId = frame.data.message_id;
      if (agent === this.root) this.#settlePending(messageId);
      const parentId = agent.info.parentId;
      if (parentId !== null) {
        this.agents.get(parentId)?.linkSubagentMessage(agent.info.agentId, frame.data.turn_number, messageId);
      }
    }
  }

  /** Add a message the client has submitted but the server has not admitted yet. It stays in
   *  `pending` until the submit returns. */
  addSending(message: Message): void {
    this.#setPending([...this.pending, message]);
  }

  /** The server admitted a `sending` message under `messageId`. */
  admitSending(localId: string, messageId: string): void {
    const sent = this.pending.find((m) => m.id === localId);
    if (!sent) return;
    const root = this.root;
    const admitted: Message = { ...sent, id: messageId, status: "accepted" };
    if (root?.has(messageId)) {
      this.#setPending(this.pending.filter((m) => m !== sent));
    } else if (root) {
      // The stream usually delivers its `message_accepted` first; if it has not yet, the
      // message joins the list now and that event fills in the rest when it arrives.
      root.addMessage(admitted);
      this.#setPending(this.pending.filter((m) => m !== sent));
    } else {
      // Nothing has arrived from the stream yet, so there is no root to add it to: it waits
      // here, under its real id, for its own `message_accepted`.
      this.#setPending(this.pending.map((m) => (m === sent ? admitted : m)));
    }
  }

  /** A `sending` message's submit failed. It stays in `pending`, with the error. */
  failSending(localId: string, error: string): void {
    this.#setPending(this.pending.map((m) => (m.id === localId ? { ...m, status: "error", error } : m)));
  }

  /** Drop a failed `sending` message from `pending`. */
  dismissSending(localId: string): void {
    this.#setPending(this.pending.filter((m) => m.id !== localId));
  }

  /** The agent a tool call belongs to, found by its `tool_id`. */
  toolAgent(toolId: string): AgentInfo | null {
    for (const agent of this.agents.values()) if (agent.hasTool(toolId)) return agent.info;
    return null;
  }

  /** Callback tool calls waiting on a client, anywhere in the tree. */
  awaitingClient(): Array<{ part: ToolPart; agent: AgentInfo }> {
    return [...this.agents.values()].flatMap((agent) =>
      agent.awaitingClient().map((part) => ({ part, agent: agent.info }))
    );
  }

  /** Everything changed since the last call, ready to publish. */
  takeChanges(): ProjectionChanges {
    const agents: Array<[string, AgentChanges]> = [];
    for (const [id, agent] of this.agents) {
      const changes = agent.takeChanges();
      if (changes) agents.push([id, changes]);
    }
    const pending = this.#pendingTouched ? this.pending : null;
    this.#pendingTouched = false;
    return { agents, pending };
  }

  #agent(agentId: string): AgentProjection {
    let agent = this.agents.get(agentId);
    if (!agent) {
      agent = new AgentProjection({
        agentId,
        parentId: null,
        agentKey: null,
        workflowId: null,
        lifecycle: "running",
        unavailable: null
      });
      this.agents.set(agentId, agent);
    }
    return agent;
  }

  #settlePending(messageId: string): void {
    if (this.pending.some((m) => m.id === messageId)) {
      this.#setPending(this.pending.filter((m) => m.id !== messageId));
    }
  }

  #setPending(pending: readonly Message[]): void {
    this.pending = pending;
    this.#pendingTouched = true;
  }
}

/**
 * One fragment of a `thought_summary` delta's text. The delta is whatever the model provider
 * dumped, so its shape is the provider's: Gemini `{content: {text}}`, OpenAI `{delta}`,
 * Pydantic AI `{content}` or `{content_delta}`.
 */
export function thoughtDeltaText(delta: JsonRecord): string {
  const asText = (value: unknown): string => (typeof value === "string" ? value : "");
  const content = delta.content;
  if (typeof content === "object" && content !== null && "text" in content) {
    return asText((content as { text?: unknown }).text);
  }
  return asText(content) || asText(delta.content_delta) || asText(delta.delta);
}

function clone(value: JsonValue): JsonValue {
  return value === null || typeof value !== "object" ? value : (JSON.parse(JSON.stringify(value)) as JsonValue);
}
