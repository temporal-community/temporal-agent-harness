// What a session publishes about each agent in its tree, and how one flush's changes apply to
// it. Shared by every SessionState implementation, so a binding only chooses where to keep the
// result.

import type { HarnessMessage } from "./messages.ts";
import type { AgentInfo } from "./projection.ts";
import type { AgentSchema, UntypedAgent } from "./schema.ts";

/** One agent in the session's tree: the root, or a subagent at any depth. */
export interface AgentView<A extends AgentSchema = UntypedAgent> extends AgentInfo {
  /** Whether it has a turn open. */
  agentStatus: "idle" | "busy";
  /** Every message admitted to it, in the order its stream admitted them. */
  messages: readonly HarnessMessage<A>[];
  /** Its observable state documents, by state id, once their snapshots arrive. */
  states: Readonly<Partial<A["states"]>>;
}

/** One agent's changes in one flush. */
export interface AgentUpdate {
  info?: AgentInfo;
  /** Replace or append messages by index. An index equal to the current length appends. */
  messages?: ReadonlyArray<readonly [number, HarnessMessage]>;
  states?: ReadonlyArray<readonly [string, unknown]>;
  agentStatus?: "idle" | "busy";
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
    agentStatus: "idle",
    messages: [],
    states: {}
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
  return {
    ...base,
    ...update.info,
    agentStatus: update.agentStatus ?? base.agentStatus,
    messages,
    states
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
  agentStatus: "idle",
  messages: Object.freeze([]) as readonly HarnessMessage[],
  states: Object.freeze({})
}) as AgentView;
