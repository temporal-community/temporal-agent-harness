// Starting a session: `startSession(starter, Agent, { data })`, typed by the agent's generated
// definition so its init data is required, optional, or refused exactly as its `@agent.init`
// declares.

import type { AgentSchema, UntypedAgent } from "./schema.ts";

/** An agent to start: its workflow type, and (as a type only) its generated schema. Codegen
 *  emits one alongside each agent's interface, under the same name. */
export interface AgentDefinition<A extends AgentSchema = UntypedAgent> {
  readonly workflowType: string;
  /** Never set at runtime; it is how `startSession` learns `A` from the definition. */
  readonly schema?: A;
}

/** What `POST /api/sessions` sends: the fields `startSession` fills in. */
export interface CreateSessionRequest {
  agent_workflow_type: string;
  session_id?: string;
  data?: unknown;
}

/** A session the harness server knows about, as `POST` / `GET /api/sessions` return it. */
export interface SessionSummary {
  workflow_id: string;
  created_at: number;
  label: string;
  agent_workflow_type: string;
  execution_status?: string;
  closed?: boolean;
}

/** Where sessions are created. `HttpTransport` is one. */
export interface SessionStarter {
  createSession(request: CreateSessionRequest, signal?: AbortSignal): Promise<SessionSummary>;
}

/** The init data option for agent `A`: required, optional, or not accepted. */
export type InitDataOption<A extends AgentSchema> = A["initData"] extends {
  data: infer D;
  required: true;
}
  ? { data: D }
  : A["initData"] extends { data: infer D }
    ? { data?: D }
    : { data?: never };

export type StartSessionOptions<A extends AgentSchema> = InitDataOption<A> & {
  /** A caller-chosen session id; creation is idempotent when it is set. */
  sessionId?: string;
  signal?: AbortSignal;
};

type StartArgs<A extends AgentSchema> = A["initData"] extends { required: true }
  ? [options: StartSessionOptions<A>]
  : [options?: StartSessionOptions<A>];

/** Start a session for `agent`, sending the init data its `@agent.init` takes. The server
 *  rejects data that does not fit with an `HttpError` (status 422, code `invalid_init_data`). */
export async function startSession<A extends AgentSchema = UntypedAgent>(
  starter: SessionStarter,
  agent: AgentDefinition<A>,
  ...[options]: StartArgs<A>
): Promise<SessionSummary> {
  const { data, sessionId, signal } = (options ?? {}) as {
    data?: unknown;
    sessionId?: string;
    signal?: AbortSignal;
  };
  const request: CreateSessionRequest = { agent_workflow_type: agent.workflowType };
  if (sessionId !== undefined) request.session_id = sessionId;
  if (data !== undefined) request.data = data;
  return starter.createSession(request, signal);
}
