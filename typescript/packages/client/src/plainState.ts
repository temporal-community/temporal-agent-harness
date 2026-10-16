// A SessionState with no framework behind it: plain fields, and a listener called after each
// write. For scripts, tests, and bindings to frameworks with an external-store hook.

import type { AgentSseFrame } from "./frames.ts";
import type { HarnessMessage } from "./messages.ts";
import type { AgentSchema, UntypedAgent } from "./schema.ts";
import type { ConnectionStatus, SessionState } from "./session.ts";
import { applyAgentUpdate, rootView, type AgentUpdate, type AgentView } from "./views.ts";

export class PlainSessionState<A extends AgentSchema = UntypedAgent> implements SessionState<A> {
  #connection: ConnectionStatus = "stopped";
  #error: Error | undefined = undefined;
  #rootAgentId: string | null = null;
  #pending: readonly HarnessMessage<A>[] = [];
  agents: Readonly<Record<string, AgentView>> = {};
  frames: readonly AgentSseFrame[] = [];
  readonly #listeners = new Set<() => void>();

  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  /** The root agent's messages. */
  get messages(): readonly HarnessMessage<A>[] {
    return rootView<A>(this).messages;
  }

  /** The root agent's observable state. */
  get states(): Readonly<Partial<A["states"]>> {
    return rootView<A>(this).states;
  }

  /** Whether the root agent has a turn open. */
  get agentStatus(): "idle" | "busy" {
    return rootView<A>(this).agentStatus;
  }

  /** Any agent in the tree by `agent_id` — e.g. a subagent part's `subagentId` — typed as `B`
   *  when its generated type is given. */
  agent<B extends AgentSchema = UntypedAgent>(agentId: string): AgentView<B> | undefined {
    return this.agents[agentId] as AgentView<B> | undefined;
  }

  get connection(): ConnectionStatus {
    return this.#connection;
  }
  set connection(value: ConnectionStatus) {
    this.#connection = value;
    this.#emit();
  }

  get error(): Error | undefined {
    return this.#error;
  }
  set error(value: Error | undefined) {
    this.#error = value;
    this.#emit();
  }

  get rootAgentId(): string | null {
    return this.#rootAgentId;
  }
  set rootAgentId(value: string | null) {
    this.#rootAgentId = value;
    this.#emit();
  }

  get pending(): readonly HarnessMessage<A>[] {
    return this.#pending;
  }
  set pending(value: readonly HarnessMessage<A>[]) {
    this.#pending = value;
    this.#emit();
  }

  updateAgents(updates: ReadonlyArray<readonly [string, AgentUpdate]>): void {
    const next = { ...this.agents };
    for (const [id, update] of updates) next[id] = applyAgentUpdate(next[id], id, update);
    this.agents = next;
    this.#emit();
  }

  appendFrames(frames: readonly AgentSseFrame[]): void {
    this.frames = this.frames.concat(frames);
    this.#emit();
  }

  #emit(): void {
    for (const listener of this.#listeners) listener();
  }
}
