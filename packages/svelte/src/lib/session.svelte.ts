// AgentSession: a reactive handle on one agent session. Construct it in a component, read its
// fields in markup or effects, and call its actions. The connection is open exactly while
// something reads it: the first effect to read any field opens it, and it closes when the last
// such effect is torn down, so unmounting the last view of a session releases its stream.

import { getContext, hasContext, setContext } from "svelte";
import { createSubscriber } from "svelte/reactivity";
import {
  AgentSessionCore,
  rootView,
  waitingCalls,
  type AgentSchema,
  type AgentSessionOptions as CoreOptions,
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

import { SvelteSessionState } from "./state.svelte.js";

export interface AgentSessionOptions<A extends AgentSchema>
  extends Omit<CoreOptions<A>, "sessionId"> {
  /** The session to follow. Pass a getter (`get sessionId() { return id; }`) to switch
   *  sessions when `id` changes. */
  readonly sessionId: string;
}

/** One session's core and state, shared by every AgentSession that follows it. */
class SessionEntry<A extends AgentSchema> {
  readonly state = new SvelteSessionState<A>();
  readonly core: AgentSessionCore<A>;
  #readers = 0;

  constructor(options: CoreOptions<A>) {
    this.core = new AgentSessionCore(options, this.state);
  }

  acquire(): void {
    if (this.#readers++ === 0) this.core.start();
  }

  release(): void {
    if (--this.#readers === 0) this.core.stop();
  }
}

/**
 * Sessions by id, so that components following the same session share one connection and one
 * state. Put one in context with `createHarnessContext()`; an AgentSession constructed outside
 * any such context gets its own.
 *
 * The first AgentSession to follow an id decides that session's options (`callbackTools`,
 * transport and so on); later ones share what it set up.
 */
export class SessionStore {
  readonly #entries = new Map<string, SessionEntry<AgentSchema>>();

  entry<A extends AgentSchema>(options: CoreOptions<A>): SessionEntry<A> {
    let entry = this.#entries.get(options.sessionId) as SessionEntry<A> | undefined;
    if (!entry) {
      entry = new SessionEntry(options);
      this.#entries.set(options.sessionId, entry as unknown as SessionEntry<AgentSchema>);
    }
    return entry;
  }
}

const STORE = Symbol("temporal-agent-harness sessions");

/** Share sessions below this component: every AgentSession constructed in its subtree for the
 *  same session id uses one connection. Call it during component initialisation. */
export function createHarnessContext(store = new SessionStore()): SessionStore {
  return setContext(STORE, store);
}

function contextStore(): SessionStore | null {
  // Outside component initialisation there is no context to read, and asking throws.
  try {
    return hasContext(STORE) ? getContext<SessionStore>(STORE) : null;
  } catch {
    return null;
  }
}

export class AgentSession<A extends AgentSchema = UntypedAgent> {
  readonly #options: AgentSessionOptions<A>;
  readonly #store: SessionStore;
  readonly #entry: SessionEntry<A> = $derived.by(() => this.#resolve());
  readonly #subscribe: () => void;

  readonly #waiting = $derived.by(() =>
    waitingCalls(this.#state().agents, (toolName) => this.#entry.core.handlesCallback(toolName))
  );

  constructor(options: AgentSessionOptions<A>, store?: SessionStore) {
    this.#options = options;
    this.#store = store ?? contextStore() ?? new SessionStore();
    this.#subscribe = createSubscriber(() =>
      // Hold the session for as long as anything reads it. The inner effect follows
      // `sessionId`, so switching sessions moves the hold from the old one to the new one.
      $effect.root(() => {
        $effect(() => {
          const entry = this.#entry;
          entry.acquire();
          return () => entry.release();
        });
      })
    );
  }

  get sessionId(): string {
    return this.#options.sessionId;
  }

  get connection(): ConnectionStatus {
    return this.#state().connection;
  }

  get error(): Error | undefined {
    return this.#state().error;
  }

  /** The root agent's messages. */
  get messages(): readonly HarnessMessage<A>[] {
    return rootView<A>(this.#state()).messages;
  }

  /** The root agent's observable state, by state id; each is undefined until its snapshot. */
  get states(): Readonly<Partial<A["states"]>> {
    return rootView<A>(this.#state()).states;
  }

  /** Whether the root agent has a turn open. */
  get agentStatus(): "idle" | "busy" {
    return rootView<A>(this.#state()).agentStatus;
  }

  /** Messages sent from here that the server has not admitted yet, and failed sends. */
  get pending(): readonly HarnessMessage<A>[] {
    return this.#state().pending;
  }

  /** Every agent in the session's tree — the root and its subagents at any depth — by id. */
  get agents(): Readonly<Record<string, AgentView>> {
    return this.#state().agents;
  }

  get rootAgentId(): string | null {
    return this.#state().rootAgentId;
  }

  /** Every frame received, from every agent. */
  get frames(): readonly AgentSseFrame[] {
    return this.#state().frames;
  }

  /** Gated tool calls waiting on a person, anywhere in the tree. */
  get pendingApprovals(): readonly WaitingToolCall[] {
    return this.#waiting.approvals;
  }

  /** Callback tool calls waiting on a client, anywhere in the tree, other than the ones the
   *  session's `callbackTools` answer. */
  get pendingCallbacks(): readonly WaitingToolCall[] {
    return this.#waiting.callbacks;
  }

  /** Any agent in the tree by id — a subagent part's `subagentId`, say — typed as `B` when its
   *  generated type is given. */
  agent<B extends AgentSchema = UntypedAgent>(agentId: string): AgentView<B> | undefined {
    return this.#state().agents[agentId] as AgentView<B> | undefined;
  }

  sendMessage<H extends HandlerName<A>>(
    handler: H,
    payload: HandlerInput<A, H>
  ): Promise<SubmitMessageResponse | null> {
    return this.#entry.core.sendMessage(handler, payload);
  }

  respondToApproval(toolId: string, decision: ToolApprovalDecision): Promise<boolean> {
    return this.#entry.core.respondToApproval(toolId, decision);
  }

  provideToolOutput(toolId: string, result: unknown): Promise<boolean> {
    return this.#entry.core.provideToolOutput(toolId, result);
  }

  provideToolError(toolId: string, error: string): Promise<boolean> {
    return this.#entry.core.provideToolError(toolId, error);
  }

  /** Stop the agent: it finishes its turn loop and auto-denies anything pending. */
  closeSession(): Promise<boolean> {
    return this.#entry.core.closeSession();
  }

  /** Drop a failed send from `pending`. */
  dismiss(localId: string): void {
    this.#entry.core.dismiss(localId);
  }

  clearError(): void {
    this.#entry.core.clearError();
  }

  #state(): SvelteSessionState<A> {
    this.#subscribe();
    return this.#entry.state;
  }

  #resolve(): SessionEntry<A> {
    const { sessionId, ...rest } = this.#options;
    return this.#store.entry<A>({ ...rest, sessionId });
  }
}
