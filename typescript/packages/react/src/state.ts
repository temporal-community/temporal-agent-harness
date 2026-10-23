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
} from "@temporalio/agent-harness-client";

export interface SessionSnapshot<A extends AgentSchema = UntypedAgent> {
  readonly connection: ConnectionStatus;
  readonly error: Error | undefined;
  readonly rootAgentId: string | null;
  readonly agents: Readonly<Record<string, AgentView>>;
  readonly pending: readonly HarnessMessage<A>[];
  readonly frames: readonly AgentSseFrame[];
}

type SnapshotFields<A extends AgentSchema> = Omit<SessionSnapshot<A>, "frames"> & { readonly frameCount: number };

/** A snapshot whose frames are the first `frameCount` of the state's append-only log, copied out
 *  only when read: every flush makes a snapshot, and copying the whole log into each would make
 *  a flush cost more the longer the session ran. */
class Snapshot<A extends AgentSchema> implements SessionSnapshot<A> {
  declare readonly connection: ConnectionStatus;
  declare readonly error: Error | undefined;
  declare readonly rootAgentId: string | null;
  declare readonly agents: Readonly<Record<string, AgentView>>;
  declare readonly pending: readonly HarnessMessage<A>[];
  declare readonly frameCount: number;
  readonly #log: readonly AgentSseFrame[];
  #frames: readonly AgentSseFrame[] | undefined;

  constructor(fields: SnapshotFields<A>, log: readonly AgentSseFrame[]) {
    Object.assign(this, fields);
    this.#log = log;
  }

  get frames(): readonly AgentSseFrame[] {
    return (this.#frames ??= this.#log.slice(0, this.frameCount));
  }
}

export class ReactSessionState<A extends AgentSchema = UntypedAgent> implements SessionState<A> {
  readonly #log: AgentSseFrame[] = [];
  #snapshot = new Snapshot<A>(
    { connection: "stopped", error: undefined, rootAgentId: null, agents: {}, pending: [], frameCount: 0 },
    this.#log
  );
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

  updateAgents(updates: ReadonlyArray<readonly [string, AgentUpdate]>): void {
    const next = { ...this.#snapshot.agents };
    for (const [id, update] of updates) next[id] = applyAgentUpdate(next[id], id, update);
    this.#set("agents", next);
  }

  appendFrames(frames: readonly AgentSseFrame[]): void {
    for (const frame of frames) this.#log.push(frame);
    this.#set("frameCount", this.#log.length);
  }

  #set<K extends keyof SnapshotFields<A>>(key: K, value: SnapshotFields<A>[K]): void {
    if (this.#snapshot[key] === value) return;
    this.#snapshot = new Snapshot<A>({ ...this.#snapshot, [key]: value }, this.#log);
    for (const listener of this.#listeners) listener();
  }
}
