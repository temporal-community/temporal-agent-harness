import type {
  AcceptedMessageTypesResponse,
  AgentInterfaceFunction,
  AgentRegistryResponse,
  AgentSseFrame,
  ChatRequest,
  CreateSessionRequest,
  CreateSessionResponse,
  Session,
  SubmitMessageResponse,
  ToolApprovalRequest,
  ToolApprovalResponse,
  WorkflowId
} from "./types";

export interface AgentApi {
  listAgents(): Promise<AgentRegistryResponse>;
  listSessions(): Promise<Session[]>;
  createSession(request: CreateSessionRequest): Promise<CreateSessionResponse>;
  acceptedMessageTypes(sessionId: WorkflowId): Promise<AcceptedMessageTypesResponse>;
  agentInterface(sessionId: WorkflowId): Promise<AgentInterfaceFunction[]>;
  attach(sessionId: WorkflowId, fromOffset?: number, signal?: AbortSignal): AsyncIterable<AgentSseFrame>;
  submitMessage(request: ChatRequest, signal?: AbortSignal): Promise<SubmitMessageResponse>;
  chat(request: ChatRequest, signal?: AbortSignal): AsyncIterable<AgentSseFrame>;
  approve(request: ToolApprovalRequest): Promise<ToolApprovalResponse>;
}
