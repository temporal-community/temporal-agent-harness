import type {
  CallbackResultRequest,
  CallbackResultResponse,
  AgentInterfaceFunction,
  AgentStatusResponse,
  AgentRegistration,
  AgentRegistryResponse,
  AgentSseFrame,
  ChatRequest,
  CreateSessionRequest,
  CreateSessionResponse,
  FileChunk,
  FileViewRequest,
  OKFGraph,
  OKFGraphRequest,
  RegisterAgentsResponse,
  Session,
  SessionsPage,
  SubmitMessageResponse,
  ToolApprovalRequest,
  ToolApprovalResponse,
  WorkflowExecutionState,
  WorkflowId
} from "./types";

export interface AgentApi {
  listAgents(): Promise<AgentRegistryResponse>;
  /** Register every agent, or none: a key registered again is replaced and made active. */
  registerAgents(agents: AgentRegistration[]): Promise<RegisterAgentsResponse>;
  /** The agent as it was registered, which registers it again when sent back. */
  agentRegistration(key: string): Promise<AgentRegistration>;
  /** Stop the agent from starting new sessions. Its sessions stay listed and usable. */
  retireAgent(key: string): Promise<void>;
  /** Remove the agent from the registry. Its sessions leave the session list. */
  deleteAgent(key: string): Promise<void>;
  /** One page of sessions, newest first: the first without a token, else the one it names. */
  listSessions(pageToken?: string): Promise<SessionsPage>;
  /** One session by id, wherever it falls in the list; null when there is no such session. */
  getSession(sessionId: WorkflowId): Promise<Session | null>;
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
  provideCallbackResult(request: CallbackResultRequest): Promise<CallbackResultResponse>;
  /**
   * One page of a file in an activity-backed Code Mode mount, read from its store as it is
   * NOW: not as it was at any point in the session. Runs outside the agent's workflow.
   */
  viewFile(request: FileViewRequest): Promise<FileChunk>;
  /** An OKF bundle mount's whole graph, walked from its store as it is now. */
  okfGraph(request: OKFGraphRequest): Promise<OKFGraph>;
}
