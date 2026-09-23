// The SessionState the core writes into, as Svelte state. `$state.raw` rather than deep
// `$state`: the core always hands over whole new objects, so deep proxies would buy nothing and
// make replaying a long session expensive.

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

export class SvelteSessionState<A extends AgentSchema = UntypedAgent> implements SessionState<A> {
  connection = $state<ConnectionStatus>("stopped");
  error = $state.raw<Error | undefined>(undefined);
  rootAgentId = $state<string | null>(null);
  agents = $state.raw<Readonly<Record<string, AgentView>>>({});
  pending = $state.raw<readonly HarnessMessage<A>[]>([]);
  frames = $state.raw<readonly AgentSseFrame[]>([]);

  updateAgents(updates: ReadonlyArray<readonly [string, AgentUpdate]>): void {
    const next = { ...this.agents };
    for (const [id, update] of updates) next[id] = applyAgentUpdate(next[id], id, update);
    this.agents = next;
  }

  appendFrames(frames: readonly AgentSseFrame[]): void {
    this.frames = this.frames.concat(frames);
  }
}
