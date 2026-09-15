import type {
  AgentInterfaceFunction,
  AgentStatusResponse,
  AgentRegistryResponse,
  AgentSseFrame,
  ChatRequest,
  CreateSessionRequest,
  CreateSessionResponse,
  Session,
  SubmitMessageResponse,
  ToolApprovalRequest,
  ToolApprovalResponse,
  WorkflowExecutionState,
  WorkflowId
} from "./types";

export interface AgentApi {
  listAgents(): Promise<AgentRegistryResponse>;
  listSessions(): Promise<Session[]>;
  createSession(request: CreateSessionRequest): Promise<CreateSessionResponse>;
  workflowStatus(workflowId: WorkflowId): Promise<WorkflowExecutionState>;
  agentInterface(sessionId: WorkflowId): Promise<AgentInterfaceFunction[]>;
  agentStatus(sessionId: WorkflowId): Promise<AgentStatusResponse>;
  /**
   * Stop an agent via the harness `close` signal: it winds down its turn loop, drains
   * in-flight work, and auto-denies pending approvals/callbacks. This is a first-class
   * control-plane action, not a message — it is deliberately NOT an agent handler, so it
   * works on any agent regardless of what it accepts.
   */
  closeSession(sessionId: WorkflowId): Promise<void>;
  attach(sessionId: WorkflowId, fromOffset?: number, signal?: AbortSignal): AsyncIterable<AgentSseFrame>;
  submitMessage(request: ChatRequest, signal?: AbortSignal): Promise<SubmitMessageResponse>;
  chat(request: ChatRequest, signal?: AbortSignal): AsyncIterable<AgentSseFrame>;
  approve(request: ToolApprovalRequest): Promise<ToolApprovalResponse>;
}
