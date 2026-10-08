import type {
  CallbackResultRequest,
  CallbackResultResponse,
  AgentInterfaceFunction,
  AgentStatusResponse,
  AgentRegistryResponse,
  AgentSseFrame,
  ChatRequest,
  CreateSessionRequest,
  CreateSessionResponse,
  FileChunk,
  FileViewRequest,
  OKFGraph,
  OKFGraphRequest,
  Session,
  SubmitMessageResponse,
  ToolApprovalRequest,
  ToolApprovalResponse,
  WorkflowExecutionState,
  WorkflowId
} from "./types";
import type { AgentApi } from "./client";

function apiPath(path: string): string {
  return `api/${path.replace(/^\/+/, "")}`;
}

/** A failed request, carrying the server's `error` code alongside its message. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string | null
  ) {
    super(message);
  }
}

/**
 * The approval was answered before this request landed — by another tab, or by an
 * "Always allow" rule. The decision stands; this one simply arrived second.
 */
export function approvalAlreadyResolved(error: unknown): boolean {
  return error instanceof ApiError && error.code === "ToolApprovalAlreadyResolved";
}

/** The callback was fulfilled (or timed out) before this result landed — e.g. by a CLI client. */
export function callbackAlreadyResolved(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.code === "CallbackAlreadyResolved" || error.code === "UnknownCallback")
  );
}

async function json<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  if (!response.ok) {
    throw await responseError(response, `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

const MAX_ERROR_LENGTH = 500;

/** FastAPI's `detail`: a string from `HTTPException`, or a 422's list of `{loc, msg}`. */
function detailText(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return null;
  const messages = detail
    .map((item) => (typeof item === "string" ? item : item?.msg))
    .filter((msg): msg is string => typeof msg === "string" && msg.trim() !== "");
  return messages.length > 0 ? messages.join("; ") : null;
}

/**
 * The readable part of a failed response: the harness's `message`, FastAPI's `detail`, or a
 * plain-text body, cut to a length a card can hold. A proxy's HTML error page is markup, not
 * a message, so it falls back to the status line.
 */
export function errorFromBody(body: string, fallback: string): ApiError {
  let message: string | null = null;
  let code: string | null = null;
  try {
    const parsed = JSON.parse(body) as { message?: unknown; error?: unknown; detail?: unknown };
    code = typeof parsed?.error === "string" ? parsed.error : null;
    message =
      typeof parsed?.message === "string" && parsed.message.trim()
        ? parsed.message
        : detailText(parsed?.detail);
  } catch {
    message = /^\s*</.test(body) ? null : body;
  }
  message = message?.trim() || fallback;
  const chars = Array.from(message);
  if (chars.length > MAX_ERROR_LENGTH) message = `${chars.slice(0, MAX_ERROR_LENGTH).join("")}…`;
  return new ApiError(message, code);
}

async function responseError(response: Response, fallback: string): Promise<ApiError> {
  return errorFromBody(await response.text(), fallback);
}

async function* readSse(response: Response): AsyncIterable<AgentSseFrame> {
  if (!response.body) return;
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let index = buffer.indexOf("\n\n");
    while (index !== -1) {
      const frame = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const event = frame.match(/^event: (.+)$/m)?.[1];
      const data = frame.match(/^data: (.+)$/m)?.[1];
      if (event && data) yield { event, data: JSON.parse(data) } as AgentSseFrame;
      index = buffer.indexOf("\n\n");
    }
  }
}

export class HttpAgentApi implements AgentApi {
  async listAgents(): Promise<AgentRegistryResponse> {
    return json<AgentRegistryResponse>(apiPath("agents"));
  }

  async listSessions(): Promise<Session[]> {
    return json<Session[]>(apiPath("sessions"));
  }

  async createSession(
    request: CreateSessionRequest
  ): Promise<CreateSessionResponse> {
    return json<CreateSessionResponse>(apiPath("sessions"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request)
    });
  }

  async workflowStatus(workflowId: WorkflowId): Promise<WorkflowExecutionState> {
    return json<WorkflowExecutionState>(
      apiPath(`workflow-status/${encodeURIComponent(workflowId)}`)
    );
  }


  async agentInterface(sessionId: WorkflowId): Promise<AgentInterfaceFunction[]> {
    return json<AgentInterfaceFunction[]>(
      apiPath(`agent-interface/${encodeURIComponent(sessionId)}`)
    );
  }

  async agentStatus(sessionId: WorkflowId): Promise<AgentStatusResponse> {
    return json<AgentStatusResponse>(
      apiPath(`status/${encodeURIComponent(sessionId)}`)
    );
  }

  async closeSession(sessionId: WorkflowId): Promise<void> {
    await json<{ ok: boolean }>(
      apiPath(`sessions/${encodeURIComponent(sessionId)}/close`),
      { method: "POST" }
    );
  }

  async *attach(
    sessionId: WorkflowId,
    fromOffset = 0,
    signal?: AbortSignal
  ): AsyncIterable<AgentSseFrame> {
    const response = await fetch(
      apiPath(`attach?session_id=${encodeURIComponent(sessionId)}&from_offset=${fromOffset}`),
      { signal }
    );
    if (!response.ok) {
      throw await responseError(response, `Attach failed (${response.status})`);
    }
    yield* readSse(response);
  }

  async submitMessage(
    request: ChatRequest,
    signal?: AbortSignal
  ): Promise<SubmitMessageResponse> {
    return json<SubmitMessageResponse>(apiPath("messages"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
      signal
    });
  }

  async *chat(request: ChatRequest, signal?: AbortSignal): AsyncIterable<AgentSseFrame> {
    const response = await fetch(apiPath("chat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
      signal
    });
    if (!response.ok) {
      throw await responseError(response, `Chat failed (${response.status})`);
    }
    yield* readSse(response);
  }

  async approve(request: ToolApprovalRequest): Promise<ToolApprovalResponse> {
    return json<ToolApprovalResponse>(apiPath("approve"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request)
    });
  }

  async provideCallbackResult(
    request: CallbackResultRequest
  ): Promise<CallbackResultResponse> {
    return json<CallbackResultResponse>(apiPath("callback-result"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request)
    });
  }

  async viewFile(request: FileViewRequest): Promise<FileChunk> {
    return json<FileChunk>(apiPath("files/view"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request)
    });
  }

  async okfGraph(request: OKFGraphRequest): Promise<OKFGraph> {
    return json<OKFGraph>(apiPath("files/okf-graph"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request)
    });
  }
}
