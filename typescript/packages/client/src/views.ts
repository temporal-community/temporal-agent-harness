// What a session publishes about each agent in its tree, and how one flush's changes apply to
// it. Shared by every SessionState implementation, so a binding only chooses where to keep the
// result.

import type { HarnessMessage, Steering, ToolPart } from "./messages.ts";
import type { AgentInfo } from "./projection.ts";
import type { AgentSchema, HandlerInput, HandlerName, UntypedAgent } from "./schema.ts";

/** One agent in the session's tree: the root, or a subagent at any depth. */
export interface AgentView<A extends AgentSchema = UntypedAgent> extends AgentInfo {
  /** Whether it has a turn open. */
  agentStatus: "idle" | "busy";
  /** Every message admitted to it, in the order its stream admitted them. */
  messages: readonly HarnessMessage<A>[];
  /** Its observable state documents, by state id, once their snapshots arrive. */
  states: Readonly<Partial<A["states"]>>;
  /** The latest turn it opened; 0 before its first. */
  turnNumber: number;
  /** Its steering in the latest turn: what is queued now, and what that turn claimed or dropped.
   *  Empty again when the next turn opens. Every turn's steering stays on its messages'
   *  `steering`. */
  steering: SteeringView<A>;
}

/**
 * One piece of steering, with the message that sent it. `input` is that message as the user sent
 * it, typed by its handler: what a UI should render. `steeredText` is what the handler queued for
 * the model, which the agent may have rewritten or added to, so show it only to show the agent's
 * version.
 */
export type SteeringEntry<A extends AgentSchema = UntypedAgent> = {
  [H in HandlerName<A>]: {
    steeringId: string;
    messageId: string;
    handler: H;
    input: HandlerInput<A, H>;
    steeredText: string;
    status: Steering["status"];
    /** The tool call whose result carried it, once claimed; null if it went in some other way. */
    toolId: string | null;
    turnNumber: number;
    enqueuedAt: number;
    settledAt: number | null;
  };
}[HandlerName<A>];

/**
 * An agent's steering in its latest turn, by where it stands. Each list keeps its identity, and
 * each entry its own, until a steer in it changes, so a memo or selector over them re-renders on
 * steering and not on every streamed chunk.
 */
export interface SteeringView<A extends AgentSchema = UntypedAgent> {
  /** Waiting for the next tool result. */
  queued: readonly SteeringEntry<A>[];
  /** Attached to model input in this turn. */
  claimed: readonly SteeringEntry<A>[];
  /** Dropped at the end of this turn: it never reached the model. */
  dropped: readonly SteeringEntry<A>[];
}

const NO_ENTRIES: readonly SteeringEntry[] = Object.freeze([]);
const NO_STEERING: SteeringView = Object.freeze({
  queued: NO_ENTRIES,
  claimed: NO_ENTRIES,
  dropped: NO_ENTRIES
});

/**
 * The steering `messages` queued in turn `turnNumber`, reusing `previous`'s entries, lists and
 * the view itself wherever nothing in them changed.
 */
export function steeringView(
  messages: readonly HarnessMessage[],
  turnNumber: number,
  previous: SteeringView = NO_STEERING
): SteeringView {
  const before = new Map<string, SteeringEntry>();
  for (const list of [previous.queued, previous.claimed, previous.dropped])
    for (const entry of list) before.set(entry.steeringId, entry);
  const next: Record<Steering["status"], SteeringEntry[]> = { enqueued: [], claimed: [], dropped: [] };
  for (const message of messages) {
    for (const steering of message.steering) {
      if (steering.turnNumber !== turnNumber) continue;
      const old = before.get(steering.steeringId);
      const same =
        old !== undefined &&
        old.status === steering.status &&
        old.toolId === steering.toolId &&
        old.settledAt === steering.settledAt;
      next[steering.status].push(
        same
          ? old
          : ({
              steeringId: steering.steeringId,
              messageId: message.id,
              handler: message.handler,
              input: message.input,
              steeredText: steering.text,
              status: steering.status,
              toolId: steering.toolId,
              turnNumber: steering.turnNumber,
              enqueuedAt: steering.enqueuedAt,
              settledAt: steering.settledAt
            } as SteeringEntry)
      );
    }
  }
  const keep = (old: readonly SteeringEntry[], fresh: SteeringEntry[]): readonly SteeringEntry[] =>
    fresh.length === 0
      ? NO_ENTRIES
      : old.length === fresh.length && old.every((entry, i) => entry === fresh[i])
        ? old
        : fresh;
  const view = {
    queued: keep(previous.queued, next.enqueued),
    claimed: keep(previous.claimed, next.claimed),
    dropped: keep(previous.dropped, next.dropped)
  };
  return view.queued === previous.queued && view.claimed === previous.claimed && view.dropped === previous.dropped
    ? previous
    : view;
}

/** One agent's changes in one flush. */
export interface AgentUpdate {
  info?: AgentInfo;
  /** Replace or append messages by index. An index equal to the current length appends. */
  messages?: ReadonlyArray<readonly [number, HarnessMessage]>;
  states?: ReadonlyArray<readonly [string, unknown]>;
  agentStatus?: "idle" | "busy";
  turnNumber?: number;
}

/** `view` with `update` applied, as a new object; parts of it that did not change are shared. */
export function applyAgentUpdate(
  view: AgentView | undefined,
  agentId: string,
  update: AgentUpdate
): AgentView {
  const base: AgentView = view ?? {
    agentId,
    parentId: null,
    agentKey: null,
    workflowId: null,
    lifecycle: "running",
    unavailable: null,
    closeReason: null,
    agentStatus: "idle",
    messages: [],
    states: {},
    turnNumber: 0,
    steering: NO_STEERING
  };
  let messages = base.messages;
  if (update.messages && update.messages.length > 0) {
    const next = messages.slice();
    for (const [index, message] of update.messages) next[index] = message;
    messages = next;
  }
  const states =
    update.states && update.states.length > 0
      ? { ...base.states, ...Object.fromEntries(update.states) }
      : base.states;
  const turnNumber = update.turnNumber ?? base.turnNumber;
  const steering =
    messages === base.messages && turnNumber === base.turnNumber
      ? base.steering
      : steeringView(messages, turnNumber, base.steering);
  return {
    ...base,
    ...update.info,
    agentStatus: update.agentStatus ?? base.agentStatus,
    messages,
    states,
    turnNumber,
    steering
  };
}

/** The root agent's view, typed as the session's agent; empty until its first frame arrives. */
export function rootView<A extends AgentSchema>(state: {
  rootAgentId: string | null;
  agents: Readonly<Record<string, AgentView>>;
}): AgentView<A> {
  const view = state.rootAgentId === null ? undefined : state.agents[state.rootAgentId];
  return (view ?? EMPTY_ROOT) as unknown as AgentView<A>;
}

const EMPTY_ROOT: AgentView = Object.freeze({
  agentId: "",
  parentId: null,
  agentKey: null,
  workflowId: null,
  lifecycle: "running",
  unavailable: null,
  closeReason: null,
  agentStatus: "idle",
  messages: Object.freeze([]) as readonly HarnessMessage[],
  states: Object.freeze({}),
  turnNumber: 0,
  steering: NO_STEERING
}) as AgentView;

/** A tool call somewhere in the session's tree that is waiting on this client. */
export interface WaitingToolCall {
  agentId: string;
  messageId: string;
  part: ToolPart;
}

/**
 * Every tool call in the tree waiting on a person: gated calls awaiting approval (including
 * ones an evaluator is still deciding, which a person can answer first), and callback calls
 * other than those `handled` names, which the session fulfils itself.
 */
export function waitingCalls(
  agents: Readonly<Record<string, AgentView>>,
  handled: (toolName: string) => boolean
): { approvals: WaitingToolCall[]; callbacks: WaitingToolCall[] } {
  const approvals: WaitingToolCall[] = [];
  const callbacks: WaitingToolCall[] = [];
  for (const agent of Object.values(agents)) {
    for (const message of agent.messages) {
      for (const part of message.parts) {
        if (part.type !== "tool") continue;
        const call = { agentId: agent.agentId, messageId: message.id, part };
        if (part.state === "awaiting_approval" || part.state === "evaluating") approvals.push(call);
        else if (part.state === "awaiting_callback" && !handled(part.toolName)) callbacks.push(call);
      }
    }
  }
  return { approvals, callbacks };
}
