// How a session reaches its agent. `SessionTransport` is everything the session core needs;
// `HttpTransport` implements it over the harness web API (`temporal_agent_harness.web.app`).

import type { AgentSseFrame, MessageDisposition } from "./frames.ts";

export interface SubmitMessageResponse {
  turn_number: number;
  turn_id: string;
  message_id: string;
  accepted_offset: number;
  disposition: MessageDisposition;
}

/** The subset of `GET /api/status/{session_id}` the session core reads. */
export interface AgentStatusSnapshot {
  current_turn: number;
  turn_active: boolean;
  pending_turns: unknown[];
}

export interface WorkflowExecutionState {
  workflow_id: string;
  execution_status: string;
  closed: boolean;
}

export interface ToolApprovalDecision {
  approved: boolean;
  reason?: string | null;
  /** Approve, and stop asking about this tool for the rest of the session. */
  remember?: boolean;
}

export interface SessionTransport {
  /** Replay the session's merged event stream from `fromOffset`, then tail it. Ends when the
   *  agent is idle and caught up. */
  attach(sessionId: string, fromOffset: number, signal: AbortSignal): AsyncIterable<AgentSseFrame>;
  submitMessage(
    sessionId: string,
    message: { type: string; payload: unknown },
    signal?: AbortSignal
  ): Promise<SubmitMessageResponse>;
  approveTool(sessionId: string, toolId: string, decision: ToolApprovalDecision): Promise<void>;
  provideCallbackResult(
    sessionId: string,
    toolId: string,
    outcome: { result: unknown } | { error: string }
  ): Promise<void>;
  agentStatus(sessionId: string, signal?: AbortSignal): Promise<AgentStatusSnapshot>;
  workflowStatus(sessionId: string, signal?: AbortSignal): Promise<WorkflowExecutionState>;
  closeSession(sessionId: string): Promise<void>;
}

export interface HttpTransportOptions {
  /** Where the harness API is mounted. Default `"api/"`, relative to the page, which is how
   *  the packaged UI is served. */
  baseUrl?: string | URL;
  fetch?: typeof fetch;
  headers?: HeadersInit;
}

export class HttpError extends Error {
  constructor(
    message: string,
    readonly status: number,
    /** The `error` code of a harness error body, e.g. `mid_turn_rejected`. */
    readonly code: string | null
  ) {
    super(message);
    this.name = "HttpError";
  }
}

export class HttpTransport implements SessionTransport {
  readonly #base: string;
  readonly #fetch: typeof fetch;
  readonly #headers: HeadersInit | undefined;

  constructor(options: HttpTransportOptions = {}) {
    const base = String(options.baseUrl ?? "api/");
    this.#base = base.endsWith("/") ? base : `${base}/`;
    this.#fetch = options.fetch ?? ((...args) => globalThis.fetch(...args));
    this.#headers = options.headers;
  }

  async *attach(
    sessionId: string,
    fromOffset: number,
    signal: AbortSignal
  ): AsyncIterable<AgentSseFrame> {
    const query = `session_id=${encodeURIComponent(sessionId)}&from_offset=${fromOffset}`;
    const response = await this.#request(`attach?${query}`, { signal });
    yield* readSse(response);
  }

  async submitMessage(
    sessionId: string,
    message: { type: string; payload: unknown },
    signal?: AbortSignal
  ): Promise<SubmitMessageResponse> {
    return this.#json("messages", { session_id: sessionId, message }, signal);
  }

  async approveTool(sessionId: string, toolId: string, decision: ToolApprovalDecision): Promise<void> {
    await this.#json("approve", { session_id: sessionId, tool_id: toolId, ...decision });
  }

  async provideCallbackResult(
    sessionId: string,
    toolId: string,
    outcome: { result: unknown } | { error: string }
  ): Promise<void> {
    await this.#json("callback-result", { session_id: sessionId, tool_id: toolId, ...outcome });
  }

  async agentStatus(sessionId: string, signal?: AbortSignal): Promise<AgentStatusSnapshot> {
    const response = await this.#request(`status/${encodeURIComponent(sessionId)}`, { signal });
    return (await response.json()) as AgentStatusSnapshot;
  }

  async workflowStatus(sessionId: string, signal?: AbortSignal): Promise<WorkflowExecutionState> {
    const path = `workflow-status/${encodeURIComponent(sessionId)}`;
    return (await (await this.#request(path, { signal })).json()) as WorkflowExecutionState;
  }

  async closeSession(sessionId: string): Promise<void> {
    await this.#request(`sessions/${encodeURIComponent(sessionId)}/close`, { method: "POST" });
  }

  async #json<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
    const response = await this.#request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal
    });
    return (await response.json()) as T;
  }

  async #request(path: string, init: RequestInit): Promise<Response> {
    const headers = new Headers(this.#headers);
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    const response = await this.#fetch(`${this.#base}${path}`, { ...init, headers });
    if (!response.ok) throw await httpError(response);
    return response;
  }
}

async function httpError(response: Response): Promise<HttpError> {
  const fallback = `${response.status} ${response.statusText}`.trim();
  const body = await response.text().catch(() => "");
  try {
    const parsed = JSON.parse(body) as { error?: unknown; message?: unknown };
    const message = typeof parsed.message === "string" && parsed.message ? parsed.message : fallback;
    return new HttpError(message, response.status, typeof parsed.error === "string" ? parsed.error : null);
  } catch {
    return new HttpError(body || fallback, response.status, null);
  }
}

/** The `event:` / `data:` frames of a `text/event-stream` body. */
export async function* readSse(response: Response): AsyncIterable<AgentSseFrame> {
  if (!response.body) return;
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += value;
      let end = buffer.indexOf("\n\n");
      while (end !== -1) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const event = /^event: (.+)$/m.exec(block)?.[1];
        const data = /^data: (.+)$/m.exec(block)?.[1];
        if (event && data) yield { event, data: JSON.parse(data) } as AgentSseFrame;
        end = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}
