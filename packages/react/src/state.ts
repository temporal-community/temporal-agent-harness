// The SessionState the core writes into, as an immutable snapshot for `useSyncExternalStore`.
// Every write replaces the snapshot, so its identity changes exactly when something did; the
// core never mutates what it has handed over, so the fields inside can be shared as they are.

import {
  applyAgentUpdate,
  type AgentSchema,
  type AgentSseFrame,
  type AgentUpdate,
  type AgentView,
  type ConnectionStatus,
  type HarnessMessage,
  type SessionState,
  type UntypedAgent
} from "@temporal-agent-harness/client";

export interface SessionSnapshot<A extends AgentSchema = UntypedAgent> {
  readonly connection: ConnectionStatus;
  readonly error: Error | undefined;
  readonly rootAgentId: string | null;
  readonly agents: Readonly<Record<string, AgentView>>;
  readonly pending: readonly HarnessMessage<A>[];
  readonly frames: readonly AgentSseFrame[];
}

export class ReactSessionState<A extends AgentSchema = UntypedAgent> implements SessionState<A> {
  #snapshot: SessionSnapshot<A> = {
    connection: "stopped",
    error: undefined,
    rootAgentId: null,
    agents: {},
    pending: [],
    frames: []
  };
  readonly #listeners = new Set<() => void>();

  /* Arrow properties: `useSyncExternalStore` resubscribes whenever these change identity. */
  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  readonly getSnapshot = (): SessionSnapshot<A> => this.#snapshot;

  get connection(): ConnectionStatus {
    return this.#snapshot.connection;
  }
  set connection(connection: ConnectionStatus) {
    this.#set("connection", connection);
  }

  get error(): Error | undefined {
    return this.#snapshot.error;
  }
  set error(error: Error | undefined) {
    this.#set("error", error);
  }

  get rootAgentId(): string | null {
    return this.#snapshot.rootAgentId;
  }
  set rootAgentId(rootAgentId: string | null) {
    this.#set("rootAgentId", rootAgentId);
  }

  get agents(): Readonly<Record<string, AgentView>> {
    return this.#snapshot.agents;
  }
  set agents(agents: Readonly<Record<string, AgentView>>) {
    this.#set("agents", agents);
  }

  get pending(): readonly HarnessMessage<A>[] {
    return this.#snapshot.pending;
  }
  set pending(pending: readonly HarnessMessage<A>[]) {
    this.#set("pending", pending);
  }

  get frames(): readonly AgentSseFrame[] {
    return this.#snapshot.frames;
  }
  set frames(frames: readonly AgentSseFrame[]) {
    this.#set("frames", frames);
  }

  updateAgents(updates: ReadonlyArray<readonly [string, AgentUpdate]>): void {
    const next = { ...this.#snapshot.agents };
    for (const [id, update] of updates) next[id] = applyAgentUpdate(next[id], id, update);
    this.#set("agents", next);
  }

  appendFrames(frames: readonly AgentSseFrame[]): void {
    this.#set("frames", this.#snapshot.frames.concat(frames));
  }

  #set<K extends keyof SessionSnapshot<A>>(key: K, value: SessionSnapshot<A>[K]): void {
    if (this.#snapshot[key] === value) return;
    this.#snapshot = { ...this.#snapshot, [key]: value };
    for (const listener of this.#listeners) listener();
  }
}
