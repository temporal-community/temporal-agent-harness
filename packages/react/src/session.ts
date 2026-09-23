// useAgentSession: one agent session as React state. Call it in a component, render its fields,
// and call its actions. The connection is open exactly while a component using the session is
// mounted: the first to mount opens it, and it closes once the last one unmounts.

import {
  createContext,
  createElement,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode
} from "react";
import {
  AgentSessionCore,
  rootView,
  waitingCalls,
  type AgentSchema,
  type AgentSessionOptions,
  type AgentSseFrame,
  type AgentView,
  type ConnectionStatus,
  type HandlerInput,
  type HandlerName,
  type HarnessMessage,
  type SubmitMessageResponse,
  type ToolApprovalDecision,
  type UntypedAgent,
  type WaitingToolCall
} from "@temporal-agent-harness/client";

import { ReactSessionState, type SessionSnapshot } from "./state.js";

/** One session's core and state, shared by every component that follows it. */
class SessionEntry<A extends AgentSchema> {
  readonly state = new ReactSessionState<A>();
  readonly core: AgentSessionCore<A>;
  #readers = 0;
  #stopScheduled = false;

  constructor(options: AgentSessionOptions<A>) {
    this.core = new AgentSessionCore(options, this.state);
  }

  acquire(): void {
    if (this.#readers++ > 0) return;
    /* A release that has not taken effect yet is cancelled rather than followed by a restart:
       StrictMode unmounts and remounts every component once in development, and that should
       not cost a reconnect. */
    if (this.#stopScheduled) this.#stopScheduled = false;
    else this.core.start();
  }

  release(): void {
    if (--this.#readers > 0) return;
    this.#stopScheduled = true;
    queueMicrotask(() => {
      if (!this.#stopScheduled) return;
      this.#stopScheduled = false;
      this.core.stop();
    });
  }
}

/**
 * Sessions by id, so that components following the same session share one connection and one
 * state. Put one in context with `<HarnessProvider>`; a component using `useAgentSession`
 * outside any provider gets a store of its own.
 *
 * The first component to follow an id decides that session's options (`callbackTools`,
 * transport and so on); later ones, and later renders, share what it set up.
 */
export class SessionStore {
  readonly #entries = new Map<string, SessionEntry<AgentSchema>>();

  entry<A extends AgentSchema>(options: AgentSessionOptions<A>): SessionEntry<A> {
    let entry = this.#entries.get(options.sessionId) as SessionEntry<A> | undefined;
    if (!entry) {
      entry = new SessionEntry(options);
      this.#entries.set(options.sessionId, entry as unknown as SessionEntry<AgentSchema>);
    }
    return entry;
  }
}

const StoreContext = createContext<SessionStore | null>(null);

/** Share sessions below this component: every `useAgentSession` in its subtree for the same
 *  session id uses one connection. */
export function HarnessProvider({ store, children }: { store?: SessionStore; children?: ReactNode }) {
  const [value] = useState(() => store ?? new SessionStore());
  return createElement(StoreContext.Provider, { value }, children);
}

/** What `useAgentSession` returns: the session's fields as of this render, and its actions. */
export interface AgentSession<A extends AgentSchema = UntypedAgent> {
  readonly sessionId: string;
  readonly connection: ConnectionStatus;
  /** The last connection or action failure, until `clearError()`. */
  readonly error: Error | undefined;
  /** The root agent's messages. */
  readonly messages: readonly HarnessMessage<A>[];
  /** The root agent's observable state, by state id; each is undefined until its snapshot. */
  readonly states: Readonly<Partial<A["states"]>>;
  /** Whether the root agent has a turn open. */
  readonly agentStatus: "idle" | "busy";
  /** Messages sent from here that the server has not admitted yet, and failed sends. */
  readonly pending: readonly HarnessMessage<A>[];
  /** Every agent in the session's tree — the root and its subagents at any depth — by id. */
  readonly agents: Readonly<Record<string, AgentView>>;
  /** The agent the session was opened for, once the stream has named it. */
  readonly rootAgentId: string | null;
  /** Every frame received, from every agent. */
  readonly frames: readonly AgentSseFrame[];
  /** Gated tool calls waiting on a person, anywhere in the tree. */
  readonly pendingApprovals: readonly WaitingToolCall[];
  /** Callback tool calls waiting on a client, anywhere in the tree, other than the ones the
   *  session's `callbackTools` answer. */
  readonly pendingCallbacks: readonly WaitingToolCall[];
  /** Any agent in the tree by id — a subagent part's `subagentId`, say — typed as `B` when its
   *  generated type is given. */
  agent<B extends AgentSchema = UntypedAgent>(agentId: string): AgentView<B> | undefined;
  sendMessage<H extends HandlerName<A>>(
    handler: H,
    payload: HandlerInput<A, H>
  ): Promise<SubmitMessageResponse | null>;
  respondToApproval(toolId: string, decision: ToolApprovalDecision): Promise<boolean>;
  provideToolOutput(toolId: string, result: unknown): Promise<boolean>;
  provideToolError(toolId: string, error: string): Promise<boolean>;
  /** Stop the agent: it finishes its turn loop and auto-denies anything pending. */
  closeSession(): Promise<boolean>;
  /** Drop a failed send from `pending`. */
  dismiss(localId: string): void;
  clearError(): void;
}

/**
 * Follow one agent session. Re-renders when anything in it changes; a changed `sessionId`
 * moves the connection to the new session.
 */
export function useAgentSession<A extends AgentSchema = UntypedAgent>(
  options: AgentSessionOptions<A>
): AgentSession<A> {
  const context = useContext(StoreContext);
  const [own] = useState(() => new SessionStore());
  const entry = (context ?? own).entry<A>(options);

  useEffect(() => {
    entry.acquire();
    return () => entry.release();
  }, [entry]);

  /* The server snapshot is the same empty state: effects never run there, so nothing connects. */
  const snapshot = useSyncExternalStore(entry.state.subscribe, entry.state.getSnapshot, entry.state.getSnapshot);
  return useMemo(() => sessionView(entry, snapshot), [entry, snapshot]);
}

function sessionView<A extends AgentSchema>(entry: SessionEntry<A>, snapshot: SessionSnapshot<A>): AgentSession<A> {
  const { core } = entry;
  const root = rootView<A>(snapshot);
  /* Walking every part of every agent is only worth doing for a render that reads the result. */
  let waiting: ReturnType<typeof waitingCalls> | undefined;
  const waitingNow = () => (waiting ??= waitingCalls(snapshot.agents, (name) => core.handlesCallback(name)));

  return {
    sessionId: core.sessionId,
    connection: snapshot.connection,
    error: snapshot.error,
    messages: root.messages,
    states: root.states,
    agentStatus: root.agentStatus,
    pending: snapshot.pending,
    agents: snapshot.agents,
    rootAgentId: snapshot.rootAgentId,
    frames: snapshot.frames,
    get pendingApprovals() {
      return waitingNow().approvals;
    },
    get pendingCallbacks() {
      return waitingNow().callbacks;
    },
    agent: <B extends AgentSchema = UntypedAgent>(agentId: string) =>
      snapshot.agents[agentId] as AgentView<B> | undefined,
    sendMessage: (handler, payload) => core.sendMessage(handler, payload),
    respondToApproval: (toolId, decision) => core.respondToApproval(toolId, decision),
    provideToolOutput: (toolId, result) => core.provideToolOutput(toolId, result),
    provideToolError: (toolId, error) => core.provideToolError(toolId, error),
    closeSession: () => core.closeSession(),
    dismiss: (localId) => core.dismiss(localId),
    clearError: () => core.clearError()
  };
}
