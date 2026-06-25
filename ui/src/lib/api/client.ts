import type {
  AcceptedMessageTypesResponse,
  AgentInterfaceFunction,
  AgentRegistryResponse,
  AgentSseFrame,
  ChatRequest,
  CreateSessionRequest,
  CreateSessionResponse,
  OperatorCommand,
  OperatorCommandRequest,
  OperatorCommandResponse,
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
  operatorInterface(sessionId: WorkflowId): Promise<OperatorCommand[]>;
  executeOperatorCommand(request: OperatorCommandRequest): Promise<OperatorCommandResponse>;
  attach(sessionId: WorkflowId, fromOffset?: number, signal?: AbortSignal): AsyncIterable<AgentSseFrame>;
  submitMessage(request: ChatRequest, signal?: AbortSignal): Promise<SubmitMessageResponse>;
  chat(request: ChatRequest, signal?: AbortSignal): AsyncIterable<AgentSseFrame>;
  approve(request: ToolApprovalRequest): Promise<ToolApprovalResponse>;
}
