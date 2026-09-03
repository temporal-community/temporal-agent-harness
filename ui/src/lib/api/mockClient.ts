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
import type { AgentApi } from "./client";
import { realisticQaScenario } from "$lib/mock/scenarios";

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

const qaInterface: AgentInterfaceFunction[] = [
  {
    name: "ask",
    description: "Ask a free-form natural-language question.",
    parameters: {
      type: "object",
      properties: {
        text: { type: "string", title: "Text" }
      },
      required: ["text"]
    },
    output: {
      type: "object",
      properties: {
        text: { type: "string", title: "Text" }
      }
    },
    mid_turn: "enqueue",
    model_callable: true
  }
];

// Deliberately varied so the composer's schema-driven rendering is exercised by the mock:
// a single-string handler (renders as the plain text box), an enum-constrained one (renders
// as a dropdown), and a multi-field one (renders as a form). None of these names is special
// to the UI — it reads them all from this discovery payload.
const montyInterface: AgentInterfaceFunction[] = [
  {
    name: "run_script",
    description: "Execute a Python script in the Monty sandbox.",
    parameters: {
      type: "object",
      properties: {
        script: { type: "string", title: "Script" }
      },
      required: ["script"]
    },
    output: {
      type: "object",
      properties: {
        text: { type: "string", title: "Text" }
      }
    },
    mid_turn: "enqueue",
    model_callable: true
  },
  {
    name: "set_model",
    description: "Set the model this session uses for subsequent turns.",
    parameters: {
      type: "object",
      title: "SetModel",
      properties: {
        model: {
          title: "Model",
          type: "string",
          enum: ["gemini-3.5-flash", "gemini-3.1-flash-lite"]
        }
      },
      required: ["model"]
    },
    output: {
      type: "object",
      properties: {
        text: { type: "string", title: "Text" }
      }
    },
    mid_turn: "accept",
    model_callable: false
  },
  {
    name: "start_batch",
    description: "Kick off a batch run. Refuses to start while the agent is busy.",
    parameters: {
      type: "object",
      title: "Batch",
      properties: {
        label: { title: "Label", type: "string" },
        size: { title: "Size", type: "integer", minimum: 1, maximum: 100, default: 10 },
        dry_run: { title: "Dry Run", type: "boolean", default: false }
      },
      required: ["label"]
    },
    output: {
      type: "object",
      properties: {
        text: { type: "string", title: "Text" }
      }
    },
    mid_turn: "reject",
    model_callable: true
  }
];

export class MockAgentApi implements AgentApi {
  #sessions: Session[] = [...realisticQaScenario.sessions];

  async listAgents(): Promise<AgentRegistryResponse> {
    return { agents: realisticQaScenario.agents };
  }

  async listSessions(): Promise<Session[]> {
    return this.#sessions;
  }

  async createSession(
    request: CreateSessionRequest
  ): Promise<CreateSessionResponse> {
    const number = this.#sessions.length + 1;
    const session: Session = {
      workflow_id: `agent-session-mock-${number}`,
      created_at: Date.now() / 1000,
      label: `Session ${number}`,
      agent_workflow_type: request.agent_workflow_type,
      is_message_queuing_enabled: Boolean(request.is_message_queuing_enabled),
      initial_user_message: null,
      execution_status: "RUNNING",
      closed: false
    };
    this.#sessions = [...this.#sessions, session];
    return session;
  }

  async workflowStatus(workflowId: WorkflowId): Promise<WorkflowExecutionState> {
    const session = this.#sessions.find((item) => item.workflow_id === workflowId);
    return {
      workflow_id: workflowId,
      execution_status: session?.execution_status ?? "RUNNING",
      closed: Boolean(session?.closed)
    };
  }


  async agentInterface(sessionId: WorkflowId): Promise<AgentInterfaceFunction[]> {
    return sessionId.toLowerCase().includes("monty") ? montyInterface : qaInterface;
  }

  async agentStatus(sessionId: WorkflowId): Promise<AgentStatusResponse> {
    return {
      current_turn: 0,
      turn_active: false,
      turn_participants: 0,
      pending_turns: [],
      pending_approvals: [],
      subagents: [],
      approval_policy: {
        dangerously_skip_all_approvals: false,
        auto_approve_inherently_safe: true,
        auto_approve_tools: []
      },
      has_custom_approval_fallback: false
    };
  }

  async closeSession(sessionId: WorkflowId): Promise<void> {
    // Mirrors the real close signal's observable effect, so the mock UI's stop control
    // behaves the same way it does against a live workflow.
    this.#sessions = this.#sessions.map((item) =>
      item.workflow_id === sessionId
        ? { ...item, execution_status: "COMPLETED", closed: true }
        : item
    );
  }



  async *attach(
    _sessionId: WorkflowId,
    fromOffset = 0,
    signal?: AbortSignal
  ): AsyncIterable<AgentSseFrame> {
    for (const item of realisticQaScenario.frames) {
      if (signal?.aborted) return;
      if (item.data.resume_offset <= fromOffset) continue;
      await sleep(40);
      if (signal?.aborted) return;
      yield item;
    }
  }

  async submitMessage(request: ChatRequest): Promise<SubmitMessageResponse> {
    await sleep(80);
    return {
      turn_number: request.expected_turn,
      turn_id: `mock-turn-${request.expected_turn}`,
      accepted_offset: 0,
      pending: false
    };
  }

  async *chat(_request: ChatRequest, signal?: AbortSignal): AsyncIterable<AgentSseFrame> {
    yield* this.attach("agent-session-mock-qa", 0, signal);
  }

  async approve(request: ToolApprovalRequest): Promise<ToolApprovalResponse> {
    await sleep(120);
    return { tool_id: request.tool_id, accepted: true };
  }
}
