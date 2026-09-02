import type {
  AcceptedMessageTypesResponse,
  AgentInterfaceFunction,
  AccountOverview,
  CatalogResponse,
  AgentRegistryResponse,
  AgentSseFrame,
  ChatRequest,
  CreateSessionRequest,
  CreateSessionResponse,
  OperatorCommand,
  OperatorCommandRequest,
  OperatorCommandResponse,
  Session,
  SubagentCloseResolution,
  SubmitMessageResponse,
  ToolApprovalRequest,
  ToolApprovalResponse,
  ToolCallRecord,
  WorkflowExecutionState,
  WorkflowId
} from "./types";

export interface AgentApi {
  accountOverview(): Promise<AccountOverview>;
  catalog(): Promise<CatalogResponse>;
  installCatalogResource(resourceId: string): Promise<void>;
  removeCatalogResource(resourceId: string): Promise<void>;
  listToolCalls(serverName: string): Promise<ToolCallRecord[]>;
  listAgents(): Promise<AgentRegistryResponse>;
  listSessions(): Promise<Session[]>;
  refreshSessions(): Promise<Session[]>;
  createSession(request: CreateSessionRequest): Promise<CreateSessionResponse>;
  closeSession(
    sessionId: WorkflowId,
    resolution?: SubagentCloseResolution
  ): Promise<void>;
  workflowStatus(workflowId: WorkflowId): Promise<WorkflowExecutionState>;
  acceptedMessageTypes(sessionId: WorkflowId): Promise<AcceptedMessageTypesResponse>;
  agentInterface(sessionId: WorkflowId): Promise<AgentInterfaceFunction[]>;
  operatorInterface(sessionId: WorkflowId): Promise<OperatorCommand[]>;
  executeOperatorCommand(request: OperatorCommandRequest): Promise<OperatorCommandResponse>;
  attach(sessionId: WorkflowId, fromOffset?: number, signal?: AbortSignal): AsyncIterable<AgentSseFrame>;
  submitMessage(request: ChatRequest, signal?: AbortSignal): Promise<SubmitMessageResponse>;
  chat(request: ChatRequest, signal?: AbortSignal): AsyncIterable<AgentSseFrame>;
  approve(request: ToolApprovalRequest): Promise<ToolApprovalResponse>;
}
