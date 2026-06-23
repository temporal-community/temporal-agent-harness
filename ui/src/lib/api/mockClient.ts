import type {
  AcceptedMessageTypesResponse,
  AgentInterfaceFunction,
  AgentRegistryResponse,
  AgentSseFrame,
  ChatRequest,
  CreateSessionRequest,
  CreateSessionResponse,
  Session,
  ToolApprovalRequest,
  ToolApprovalResponse,
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
    }
  },
  {
    name: "slash_command",
    description: "Change QA agent runtime settings with slash commands.",
    parameters: {
      type: "object",
      properties: {
        payload: { type: "object", title: "Payload" }
      },
      required: ["payload"]
    },
    output: {
      type: "object",
      properties: {
        text: { type: "string", title: "Text" }
      }
    }
  }
];

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
    }
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
      initial_user_message: null
    };
    this.#sessions = [...this.#sessions, session];
    return session;
  }

  async acceptedMessageTypes(
    _sessionId: WorkflowId
  ): Promise<AcceptedMessageTypesResponse> {
    return {
      accepts_text: true,
      models: []
    };
  }

  async agentInterface(sessionId: WorkflowId): Promise<AgentInterfaceFunction[]> {
    return sessionId.toLowerCase().includes("monty") ? montyInterface : qaInterface;
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

  async *chat(_request: ChatRequest, signal?: AbortSignal): AsyncIterable<AgentSseFrame> {
    yield* this.attach("agent-session-mock-qa", 0, signal);
  }

  async approve(request: ToolApprovalRequest): Promise<ToolApprovalResponse> {
    await sleep(120);
    return { tool_id: request.tool_id, accepted: true };
  }
}
