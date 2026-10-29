// A plain, serializable picture of a session for logs and devtools, and a dev-only hook that
// publishes it on `window.__AGENT_HARNESS__` for the browser console.

import type { AgentSseFrame } from "./frames.ts";
import type { ConnectionStatus, SessionActivity } from "./session.ts";
import { waitingCalls, type AgentView } from "./views.ts";

/** What inspectSession reads: an AgentSessionCore's `state`, or a binding's session. */
export interface InspectableSession {
  readonly connection: ConnectionStatus;
  readonly error: Error | undefined;
  readonly rootAgentId: string | null;
  readonly agents: Readonly<Record<string, AgentView>>;
  /** Only counted, so a session typed for its agent fits too. */
  readonly pending: { readonly length: number };
  readonly frames: readonly AgentSseFrame[];
}

/** An AgentSessionCore, which also knows what it asked the server for. */
export interface InspectableCore {
  readonly state: InspectableSession;
  readonly activity: SessionActivity;
}

export interface SessionInspection {
  connection: ConnectionStatus;
  error: string | null;
  rootAgentId: string | null;
  /** The last frame's resume offset: where a re-attach picks up. */
  lastOffset: number;
  frames: number;
  pendingSends: number;
  waitingApprovals: number;
  /** Including the calls `callbackTools` answers. */
  waitingCallbacks: number;
  agents: Record<string, Omit<AgentView, "messages" | "states"> & { messages: number; states: string[] }>;
  /** Only when inspecting an AgentSessionCore. */
  activity?: Omit<SessionActivity, "framesLast10s"> & {
    /** Over the last 10 seconds. */
    framesPerSecond: number;
    /** Temporal Cloud Actions these requests cost: an attach is 2 Queries and an Update, a status
     *  check a Query, and a send, approval, callback result or close one Action each. A floor:
     *  the server makes an attach's Queries and Update again for each batch of events it waits
     *  for, which the client cannot see. */
    estimatedActions: number;
  };
}

export function inspectSession(session: InspectableSession | InspectableCore): SessionInspection {
  const state = "state" in session ? session.state : session;
  const { approvals, callbacks } = waitingCalls(state.agents, () => false);
  const agents: SessionInspection["agents"] = {};
  for (const [id, { messages, states, ...info }] of Object.entries(state.agents)) {
    agents[id] = { ...info, messages: messages.length, states: Object.keys(states) };
  }
  const inspection: SessionInspection = {
    connection: state.connection,
    error: state.error?.message ?? null,
    rootAgentId: state.rootAgentId,
    lastOffset: state.frames.at(-1)?.data.resume_offset ?? 0,
    frames: state.frames.length,
    pendingSends: state.pending.length,
    waitingApprovals: approvals.length,
    waitingCallbacks: callbacks.length,
    agents
  };
  if ("activity" in session) {
    const { framesLast10s, ...a } = session.activity;
    inspection.activity = {
      ...a,
      framesPerSecond: framesLast10s / 10,
      estimatedActions: 3 * a.attaches + a.statusQueries + a.sends + a.approvals + a.callbackResults + a.closes
    };
  }
  return inspection;
}

/** For development: `window.__AGENT_HARNESS__[sessionId]()` returns `inspectSession(session)`.
 *  Returns the removal. */
export function exposeSession(sessionId: string, session: InspectableSession | InspectableCore): () => void {
  const hook = ((globalThis as { __AGENT_HARNESS__?: Record<string, () => SessionInspection> }).__AGENT_HARNESS__ ??= {});
  hook[sessionId] = () => inspectSession(session);
  return () => delete hook[sessionId];
}
