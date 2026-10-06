/**
 * AgentApi over the inbound HTTP connector for standalone Nexus operations.
 *
 * Every call starts one AgentService operation and polls it for its result. The Go web
 * server maps the agent to its Nexus endpoint and sets the service, so the browser
 * names only the agent and the operation. Sessions live in this browser's
 * localStorage. The first message starts the agent workflow.
 */
import type {
  AgentDescriptor,
  AgentInterfaceFunction,
  AgentRegistryResponse,
  AgentSseFrame,
  AgentStatusResponse,
  CallbackResultRequest,
  CallbackResultResponse,
  ChatRequest,
  CreateSessionRequest,
  CreateSessionResponse,
  JsonRecord,
  Session,
  SubmitMessageResponse,
  ToolApprovalRequest,
  ToolApprovalResponse,
  WorkflowExecutionState,
  WorkflowId
} from "./types";
import type { AgentApi } from "./client";
import { ApiError, errorFromBody } from "./httpClient";

const OPERATIONS_PATH = "nexus/operations";
const SESSIONS_KEY = "agent-harness.connector.sessions";
const CALL_TIMEOUT = "60s";
/** pollMessages returns when the agent publishes an event, so it may wait long. */
const POLL_TIMEOUT = "3600s";
/** Makes a call fail fast when no AgentService worker polls the endpoint. */
const START_TIMEOUT = "10s";

interface ConnectorGlobals {
  __AGENT_HARNESS_TRANSPORT__?: string;
  __AGENT_HARNESS_AGENTS__?: AgentDescriptor[];
}

/** The UI uses this client when the Go web server sets this global. */
export function isConnectorTransport(): boolean {
  return (
    typeof window !== "undefined" &&
    (window as ConnectorGlobals).__AGENT_HARNESS_TRANSPORT__ === "connector"
  );
}

interface StreamItem {
  topic: string;
  offset: number;
  data: string;
}

interface PollMessagesOutput {
  items: StreamItem[] | null;
  next_offset: number;
  more_ready: boolean;
  closed?: boolean;
}

interface SendMessageOutput {
  turnNumber: number;
  turnId: string;
  streamHeadOffset?: number;
  pending?: boolean;
}

interface AgentStatusOutput {
  currentTurn: number;
  turnActive: boolean;
  pendingTurns: { turnNumber: number; turnId: string; message: string }[] | null;
  pendingApprovals:
    | { toolId: string; toolName: string; toolInput: string; turnNumber: number }[]
    | null;
  subagents: { subagentId: string; agentKey: string; workflowId: string }[] | null;
  approvalPolicy: {
    dangerouslySkipAllApprovals: boolean;
    autoApproveInherentlySafe: boolean;
    autoApproveTools: string[] | null;
  };
  hasCustomApprovalFallback: boolean;
}

interface AgentInterfaceOutput {
  handlers: { name: string; description: string; parameters: string; output: string }[] | null;
}

/** The composer needs a handler before the first message starts the workflow. */
const FALLBACK_INTERFACE: AgentInterfaceFunction[] = [
  {
    name: "ask",
    description: "Send a text message to the agent.",
    parameters: {
      type: "object",
      properties: { text: { type: "string", title: "Text" } },
      required: ["text"]
    },
    output: { type: "object" },
    mid_turn: "enqueue",
    model_callable: false
  }
];

/** A 424 body is a Nexus failure. Its handler error type becomes the ApiError code. */
function operationFailure(body: string, operation: string): ApiError {
  try {
    const failure = JSON.parse(body) as { message?: string; details?: { type?: string } };
    return new ApiError(failure.message || `${operation} failed`, failure.details?.type ?? null);
  } catch {
    return errorFromBody(body, `${operation} failed`);
  }
}

async function execute<T>(
  agent: string,
  operation: string,
  input: unknown,
  { signal, timeout = CALL_TIMEOUT }: { signal?: AbortSignal; timeout?: string } = {}
): Promise<T> {
  const id = `ui-${operation}-${crypto.randomUUID()}`;
  const start = await fetch(
    `${OPERATIONS_PATH}/${encodeURIComponent(id)}?agent=${encodeURIComponent(agent)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        operation,
        input,
        scheduleToCloseTimeout: timeout,
        scheduleToStartTimeout: START_TIMEOUT
      }),
      signal
    }
  );
  if (!start.ok) throw errorFromBody(await start.text(), `${operation} failed (${start.status})`);
  const { runId } = (await start.json()) as { runId: string };
  // A poll can return before the operation closes, so poll until it has an outcome.
  while (true) {
    const poll = await fetch(
      `${OPERATIONS_PATH}/${encodeURIComponent(id)}/poll?runId=${encodeURIComponent(runId)}`,
      { signal }
    );
    if (poll.status === 424) throw operationFailure(await poll.text(), operation);
    if (!poll.ok) throw errorFromBody(await poll.text(), `${operation} failed (${poll.status})`);
    const body = (await poll.json()) as { result?: T };
    if ("result" in body) return body.result as T;
  }
}

/** Reads field 2 (`data`) of a serialized temporal.api.common.v1.Payload. */
function payloadData(bytes: Uint8Array): Uint8Array {
  let index = 0;
  const varint = (): number => {
    let value = 0;
    for (let shift = 0; ; shift += 7) {
      const byte = bytes[index++]!;
      value += (byte & 0x7f) * 2 ** shift;
      if (byte < 0x80) return value;
    }
  };
  while (index < bytes.length) {
    const tag = varint();
    const wireType = tag & 7;
    if (wireType === 0) {
      varint();
    } else if (wireType === 1) {
      index += 8;
    } else if (wireType === 5) {
      index += 4;
    } else if (wireType === 2) {
      const length = varint();
      if (tag >>> 3 === 2) return bytes.subarray(index, index + length);
      index += length;
    } else {
      throw new Error(`Unsupported protobuf wire type ${wireType}`);
    }
  }
  return new Uint8Array();
}

/** Converts one pollMessages item into the SSE frame that /api/attach sends. */
function frameFromItem(item: StreamItem): AgentSseFrame {
  const bytes = Uint8Array.from(atob(item.data), (char) => char.charCodeAt(0));
  const envelope = JSON.parse(new TextDecoder().decode(payloadData(bytes))) as {
    event: JsonRecord & { type: string };
    [key: string]: unknown;
  };
  const { event, ...metadata } = envelope;
  return {
    event: event.type,
    data: { ...event, ...metadata, event_offset: item.offset, resume_offset: item.offset + 1 }
  } as AgentSseFrame;
}

function parseJson(value: string): JsonRecord {
  try {
    return JSON.parse(value) as JsonRecord;
  } catch {
    return {};
  }
}

/**
 * A stored session.
 *
 * `started` is false until the first message is accepted. Before that, the agent has
 * no workflow, so the client does not ask the agent about it. Sessions stored without
 * the field count as started.
 *
 * `turnEnd` is the last turn whose `turn_end` this browser read, and the stream cursor
 * after it. The client polls the stream only while a later turn exists.
 */
type StoredSession = Session & {
  started?: boolean;
  turnEnd?: { turn: number; cursor: number };
};

function readSessions(): StoredSession[] {
  try {
    return JSON.parse(localStorage.getItem(SESSIONS_KEY) ?? "[]") as StoredSession[];
  } catch {
    return [];
  }
}

function writeSessions(sessions: StoredSession[]): void {
  try {
    localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions));
  } catch {
    // Sessions then last only for this page.
  }
}

export class ConnectorAgentApi implements AgentApi {
  #sessions = readSessions();
  #agents = (typeof window === "undefined"
    ? []
    : ((window as ConnectorGlobals).__AGENT_HARNESS_AGENTS__ ?? [])) as AgentDescriptor[];

  #session(sessionId: WorkflowId): StoredSession {
    const session = this.#sessions.find((item) => item.workflow_id === sessionId);
    if (!session) throw new Error(`Unknown session ${sessionId}`);
    return session;
  }

  #agentFor(sessionId: WorkflowId): string {
    return this.#session(sessionId).agent_workflow_type;
  }

  #update(sessionId: WorkflowId, change: Partial<StoredSession>): void {
    this.#sessions = this.#sessions.map((item) =>
      item.workflow_id === sessionId ? { ...item, ...change } : item
    );
    writeSessions(this.#sessions);
  }

  async listAgents(): Promise<AgentRegistryResponse> {
    return { agents: this.#agents };
  }

  /** Lists only stored sessions of agents that the web server serves now. */
  async listSessions(): Promise<Session[]> {
    const served = new Set(this.#agents.map((agent) => agent.workflow_type));
    return this.#sessions.filter((session) => served.has(session.agent_workflow_type));
  }

  async createSession(request: CreateSessionRequest): Promise<CreateSessionResponse> {
    const agent = this.#agents.find((item) => item.workflow_type === request.agent_workflow_type);
    const session: StoredSession = {
      workflow_id: crypto.randomUUID(),
      created_at: Date.now() / 1000,
      label: agent?.label ?? request.agent_workflow_type,
      agent_workflow_type: request.agent_workflow_type,
      execution_status: "RUNNING",
      closed: false,
      started: false
    };
    this.#sessions = [...this.#sessions, session];
    writeSessions(this.#sessions);
    return session;
  }

  async workflowStatus(workflowId: WorkflowId): Promise<WorkflowExecutionState> {
    return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
  }

  async agentInterface(sessionId: WorkflowId): Promise<AgentInterfaceFunction[]> {
    if (this.#session(sessionId).started === false) return FALLBACK_INTERFACE;
    let output: AgentInterfaceOutput;
    try {
      output = await execute<AgentInterfaceOutput>(
        this.#agentFor(sessionId),
        "QueryAgentInterface",
        { sessionId }
      );
    } catch (error) {
      // NOT_FOUND: the session has no workflow yet. Other errors reach the UI.
      if (error instanceof ApiError && error.code === "NOT_FOUND") return FALLBACK_INTERFACE;
      throw error;
    }
    return (output.handlers ?? []).map((handler) => ({
      name: handler.name,
      description: handler.description,
      parameters: parseJson(handler.parameters),
      output: parseJson(handler.output),
      mid_turn: "enqueue",
      model_callable: false
    }));
  }

  async #status(sessionId: WorkflowId, signal?: AbortSignal): Promise<AgentStatusOutput> {
    return execute<AgentStatusOutput>(
      this.#agentFor(sessionId),
      "QueryAgentStatus",
      { sessionId },
      { signal }
    );
  }

  async agentStatus(sessionId: WorkflowId): Promise<AgentStatusResponse> {
    const status = await this.#status(sessionId);
    return {
      current_turn: status.currentTurn,
      turn_active: status.turnActive,
      pending_turns: (status.pendingTurns ?? []).map((turn) => ({
        turn_number: turn.turnNumber,
        turn_id: turn.turnId,
        message_id: "",
        message: turn.message
      })),
      turn_participants: status.turnActive ? 1 : 0,
      pending_approvals: (status.pendingApprovals ?? []).map((approval) => ({
        tool_id: approval.toolId,
        tool_name: approval.toolName,
        tool_input: parseJson(approval.toolInput),
        turn_number: approval.turnNumber,
        short_id: ""
      })),
      subagents: (status.subagents ?? []).map((subagent) => ({
        subagent_id: subagent.subagentId,
        agent_key: subagent.agentKey,
        workflow_id: subagent.workflowId
      })),
      approval_policy: {
        dangerously_skip_all_approvals: status.approvalPolicy.dangerouslySkipAllApprovals,
        auto_approve_inherently_safe: status.approvalPolicy.autoApproveInherentlySafe,
        auto_approve_tools: status.approvalPolicy.autoApproveTools ?? [],
        auto_mode_enabled: false
      },
      has_auto_approval_evaluator: status.hasCustomApprovalFallback
    };
  }

  async closeSession(): Promise<void> {
    throw new Error("Closing a session is not available through the connector.");
  }

  /**
   * Streams the session with AgentService.pollMessages.
   *
   * pollMessages waits until the agent publishes an event, and each call holds one
   * update open on the agent workflow until then. So the client polls only while a
   * turn exists that this browser has not read to its `turn_end`: the running turn or
   * a queued turn. When the session is idle and read to the end, the stream ends. The
   * UI attaches again after the next send.
   */
  async *attach(
    sessionId: WorkflowId,
    fromOffset = 0,
    signal?: AbortSignal
  ): AsyncIterable<AgentSseFrame> {
    if (this.#session(sessionId).started === false) return;
    const agent = this.#agentFor(sessionId);
    let cursor = fromOffset;
    while (!signal?.aborted) {
      const status = await this.#status(sessionId, signal);
      const lastTurn = status.currentTurn + (status.pendingTurns?.length ?? 0);
      const read = this.#session(sessionId).turnEnd;
      if (read && read.turn >= lastTurn && cursor >= read.cursor) return;

      let turnEnded = false;
      while (!turnEnded) {
        const page = await execute<PollMessagesOutput>(
          agent,
          "PollMessages",
          { sessionId, cursor },
          { signal, timeout: POLL_TIMEOUT }
        );
        for (const item of page.items ?? []) {
          const frame = frameFromItem(item);
          yield frame;
          if (frame.event === "turn_end") {
            const turn = Number(frame.data.turn_number);
            this.#update(sessionId, { turnEnd: { turn, cursor: item.offset + 1 } });
            turnEnded ||= turn >= lastTurn;
          }
        }
        cursor = page.next_offset;
        if (page.closed) return;
      }
    }
  }

  async submitMessage(request: ChatRequest, signal?: AbortSignal): Promise<SubmitMessageResponse> {
    const [messageType, payload] =
      typeof request.message === "string"
        ? ["ask", { text: request.message }]
        : [request.message.type, (request.message.payload as JsonRecord | undefined) ?? {}];
    const accepted = await execute<SendMessageOutput>(
      this.#agentFor(request.session_id),
      "SendAgentMessage",
      { sessionId: request.session_id, msgType: messageType, payload: JSON.stringify(payload) },
      { signal }
    );
    if (this.#session(request.session_id).started === false) {
      this.#update(request.session_id, { started: true });
    }
    // AgentService does not return a message ID. The UI then does not pair the reply.
    return {
      turn_number: accepted.turnNumber,
      turn_id: accepted.turnId,
      accepted_offset: accepted.streamHeadOffset ?? 0,
      disposition: accepted.pending ? "queued" : "opened"
    } as SubmitMessageResponse;
  }

  async *chat(request: ChatRequest, signal?: AbortSignal): AsyncIterable<AgentSseFrame> {
    const accepted = await this.submitMessage(request, signal);
    yield* this.attach(request.session_id, accepted.accepted_offset, signal);
  }

  async approve(request: ToolApprovalRequest): Promise<ToolApprovalResponse> {
    const output = await execute<{ toolId: string }>(
      this.#agentFor(request.session_id),
      "ApproveToolCall",
      {
        sessionId: request.session_id,
        toolId: request.tool_id,
        approved: request.approved,
        reason: request.reason ?? undefined,
        remember: request.remember
      }
    );
    return { tool_id: output.toolId, accepted: true };
  }

  async provideCallbackResult(request: CallbackResultRequest): Promise<CallbackResultResponse> {
    const output = await execute<{ toolId: string }>(
      this.#agentFor(request.session_id),
      "ProvideCallbackResult",
      {
        sessionId: request.session_id,
        toolId: request.tool_id,
        result: request.result ?? undefined,
        error: request.error ?? undefined
      }
    );
    return { tool_id: output.toolId, accepted: true };
  }
}
