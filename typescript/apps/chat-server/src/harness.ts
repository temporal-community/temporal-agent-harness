/**
 * Typed client for the harness HTTP API (the Python `web/app.py`).
 *
 * This server runs beside that one and is needed only to reach agents from a chat platform.
 * It holds no state of its own: every fact it needs is either derivable from the platform
 * event or fetched live from here.
 */

export interface AgentDescriptor {
  key: string;
  workflow_type: string;
  task_queue: string;
  label: string;
  description: string;
  worker?: { status: string };
}

/**
 * One `@agent.accepts` handler, from `GET /api/agent-interface/{session_id}`.
 *
 * `model_callable` says whether the MODEL may call this handler. It says nothing about whether
 * a person may, so it is never used to decide what to offer a user — every handler is
 * user-addressable and every handler is listed.
 */
export interface AcceptedFunction {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  output: Record<string, unknown>;
  /** `accept` means the handler may join an already-open turn; otherwise it waits for idle. */
  mid_turn?: string;
  model_callable?: boolean;
}

export interface PendingApproval {
  tool_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  turn_number: number;
  /** Short alias for `tool_id`; what a length-capped button payload carries. */
  short_id: string;
}

/** What `submit_message` returns once the agent has admitted the message. */
export interface MessageAccepted {
  /** Stamped on every event of this message's dispatch — how a reply is picked out of the stream. */
  message_id: string;
  turn_id: string;
  turn_number: number;
  /** `opened` a new turn, `joined` the running one, or `queued` behind it. */
  disposition: 'opened' | 'joined' | 'queued';
  /** Stream offset captured at acceptance: where to start reading to catch this dispatch. */
  accepted_offset: number;
}

export interface AgentStatus {
  agent_id: string;
  turn_active: boolean;
  pending_approvals: PendingApproval[];
}

export class HarnessClient {
  constructor(private readonly baseUrl: string) {}

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await fetch(new URL(path, this.baseUrl), {
      ...init,
        headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
      });
    } catch (err) {
      throw new Error(
        `Cannot reach the harness at ${this.baseUrl} (${String(err)}). ` +
          'Is `just server` running at the repo root?',
      );
    }
    if (!response.ok) {
      throw new Error(`${init?.method ?? 'GET'} ${path} -> ${response.status} ${await response.text()}`);
    }
    return (await response.json()) as T;
  }

  async listAgents(): Promise<AgentDescriptor[]> {
    const body = await this.request<{ agents: AgentDescriptor[] }>('/api/agents');
    return body.agents;
  }

  /** The agent's handler set. Requires a live session — an agent's interface is per-session. */
  agentInterface(sessionId: string): Promise<AcceptedFunction[]> {
    return this.request<AcceptedFunction[]>(`/api/agent-interface/${encodeURIComponent(sessionId)}`);
  }

  status(sessionId: string): Promise<AgentStatus> {
    return this.request<AgentStatus>(`/api/status/${encodeURIComponent(sessionId)}`);
  }

  /**
   * Ensure the session for `sessionId` exists, returning it either way.
   *
   * `POST /api/sessions` is idempotent when given a `session_id`, so this can be called on
   * every inbound event without checking first — which matters because a webhook handler
   * could not check without racing its own redeliveries. Starting the workflow sends the
   * agent nothing; it comes up idle, which is what makes a help guide available before the
   * user has said anything.
   */
  ensureSession(sessionId: string, workflowType: string): Promise<{ workflow_id: string }> {
    return this.request<{ workflow_id: string }>('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ agent_workflow_type: workflowType, session_id: sessionId }),
    });
  }

  /**
   * Deliver one complete message. Does NOT stream — see `attach`.
   *
   * This is `submit_message`, not `send_message`, and the distinction is load-bearing.
   * `POST /api/chat` streams the turn it opens, and therefore FAILS with `joined_turn` when a
   * `mid_turn: accept` handler joins a turn that is already running — which is exactly when
   * you most want to use one (changing the approval policy while the agent works). The harness
   * contract for an interactive client is: send every message this way, and observe it on a
   * separate `attach`.
   *
   * `requestId` becomes the update's idempotency key, so a redelivered platform event
   * re-issues the SAME update and gets the original acceptance back.
   */
  submitMessage(
    sessionId: string,
    message: string | { type: string; payload: Record<string, unknown> },
    requestId: string,
  ): Promise<MessageAccepted> {
    return this.request<MessageAccepted>('/api/messages', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId, message, request_id: requestId }),
    });
  }

  /**
   * Resolve one pending tool approval.
   *
   * `remember` is only meaningful with `approved: true`. It adds the tool to the session's
   * approval allow-list, so future calls skip the gate AND every other call of that tool
   * currently waiting resolves itself — which is why the caller has to notice that other
   * prompts just went stale.
   */
  approve(
    sessionId: string,
    toolId: string,
    approved: boolean,
    remember = false,
  ): Promise<unknown> {
    return this.request('/api/approve', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId, tool_id: toolId, approved, remember }),
    });
  }

  /**
   * Whether the session workflow exists and is running.
   *
   * Three answers, not two. "There is no session" and "I could not find out" are different
   * facts, and collapsing them into `false` makes a transient harness blip look exactly like
   * a brand-new thread — which then triggers first-contact behaviour and a second call that
   * fails loudly, reporting an error for a turn that otherwise succeeds.
   *
   * Reads `closed`, which the endpoint reports directly. The status field is named
   * `execution_status`, NOT `status`.
   */
  async sessionState(sessionId: string): Promise<'live' | 'absent' | 'unknown'> {
    try {
      const state = await this.request<{ execution_status?: string; closed?: boolean }>(
        `/api/workflow-status/${encodeURIComponent(sessionId)}`,
      );
      return state.closed === false ? 'live' : 'absent';
    } catch (err) {
      // A well-formed "not found" is an answer; anything else means we could not ask.
      const message = err instanceof Error ? err.message : String(err);
      return /\b404\b|NOT_FOUND/.test(message) ? 'absent' : 'unknown';
    }
  }

  /** Attach to the session's event stream from `fromOffset`. */
  async *attach(
    sessionId: string,
    fromOffset: number,
  ): AsyncGenerator<{ event: string; data: Record<string, unknown> }> {
    const url = new URL('/api/attach', this.baseUrl);
    url.searchParams.set('session_id', sessionId);
    url.searchParams.set('from_offset', String(fromOffset));

    const response = await fetch(url, { headers: { accept: 'text/event-stream' } });
    if (!response.ok || !response.body) throw new Error(`attach ${sessionId} -> ${response.status}`);
    yield* parseSse(response.body);
  }
}

/** Decode an SSE body into `{event, data}` frames, tolerating frames split across reads. */
async function* parseSse(
  body: ReadableStream<BufferSource>,
): AsyncGenerator<{ event: string; data: Record<string, unknown> }> {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += value;

      let split: number;
      while ((split = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);

        let name = 'message';
        const dataLines: string[] = [];
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) name = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
        }
        if (dataLines.length === 0) continue;
        try {
          yield { event: name, data: JSON.parse(dataLines.join('\n')) as Record<string, unknown> };
        } catch {
          // One unparseable frame is not worth killing the turn's stream over.
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
}
