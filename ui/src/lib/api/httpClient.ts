import type {
  AcceptedMessageTypesResponse,
  AgentInterfaceFunction,
  AgentRegistryResponse,
  AgentSseFrame,
  ChatRequest,
  CreateSessionRequest,
  CreateSessionResponse,
  OperatorCommand,
  Session,
  SubmitMessageResponse,
  ToolApprovalRequest,
  ToolApprovalResponse,
  WorkflowId
} from "./types";
import type { AgentApi } from "./client";

function apiPath(path: string): string {
  return `api/${path.replace(/^\/+/, "")}`;
}

async function json<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, `Request failed (${response.status})`));
  }
  return response.json() as Promise<T>;
}

async function responseErrorMessage(response: Response, fallback: string): Promise<string> {
  const body = await response.text();
  if (!body) return fallback;
  try {
    const parsed = JSON.parse(body) as { message?: unknown };
    return typeof parsed.message === "string" && parsed.message.trim()
      ? parsed.message
      : body;
  } catch {
    return body;
  }
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

  async acceptedMessageTypes(
    sessionId: WorkflowId
  ): Promise<AcceptedMessageTypesResponse> {
    const functions = await this.agentInterface(sessionId);
    return {
      accepts_text: functions.some((item) => item.name === "ask"),
      models: functions.map((item) => ({
        name: item.name,
        json_schema: item.parameters
      }))
    };
  }

  async agentInterface(sessionId: WorkflowId): Promise<AgentInterfaceFunction[]> {
    return json<AgentInterfaceFunction[]>(
      apiPath(`agent-interface/${encodeURIComponent(sessionId)}`)
    );
  }

  async operatorInterface(sessionId: WorkflowId): Promise<OperatorCommand[]> {
    return json<OperatorCommand[]>(
      apiPath(`operator-interface/${encodeURIComponent(sessionId)}`)
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
      throw new Error(await responseErrorMessage(response, `Attach failed (${response.status})`));
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
      throw new Error(await responseErrorMessage(response, `Chat failed (${response.status})`));
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
}
