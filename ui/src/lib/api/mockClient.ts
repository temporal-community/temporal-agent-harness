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

const harnessOperatorInterface: OperatorCommand[] = [
  {
    name: "approvals",
    payload_name: "set-approvals",
    label: "/approvals",
    description: "Set the tool approval policy for this session.",
    aliases: ["set-approvals"],
    argument: {
      kind: "enum",
      required: true,
      choices: ["strict", "safe", "skip"],
      placeholder: "strict | safe | skip",
      allow_multiple: false
    },
    source: "harness"
  },
  {
    name: "allow-tools",
    payload_name: "allow-tools",
    label: "/allow-tools",
    description: "Auto-approve one or more named tools for this session.",
    aliases: ["allow-tool"],
    argument: {
      kind: "tool_names",
      required: true,
      choices: [],
      placeholder: "tool_name",
      allow_multiple: true
    },
    source: "harness"
  },
  {
    name: "status",
    payload_name: "status",
    label: "/status",
    description: "Show the current harness status for this session.",
    aliases: [],
    argument: null,
    source: "harness"
  }
];

const montyOperatorInterface: OperatorCommand[] = [
  ...harnessOperatorInterface,
  {
    name: "model",
    payload_name: "set-model",
    label: "/model",
    description: "Set the model for this Monty session.",
    aliases: [],
    argument: {
      kind: "enum",
      required: true,
      choices: ["gemini-3.5-flash", "gemini-3.1-flash-lite"],
      placeholder: "model",
      allow_multiple: false
    },
    source: "agent"
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

  async operatorInterface(sessionId: WorkflowId): Promise<OperatorCommand[]> {
    return sessionId.toLowerCase().includes("monty")
      ? montyOperatorInterface
      : harnessOperatorInterface;
  }

  async executeOperatorCommand(
    request: OperatorCommandRequest
  ): Promise<OperatorCommandResponse> {
    await sleep(80);
    if (request.name === "set-approvals") {
      return { text: `Approvals set to **${request.arg ?? "strict"}**.` };
    }
    if (request.name === "allow-tools") {
      const noun = request.arg?.includes(",") ? "Tools" : "Tool";
      return {
        text: `${noun} \`${request.arg ?? ""}\` will be auto-approved.`
      };
    }
    if (request.name === "set-model") {
      return { text: `Model set to **${request.arg ?? "gemini-3.5-flash"}**.` };
    }
    if (request.name === "status") {
      return { text: "- Agent id: `mock`\n- Turn: `0` (idle)\n- Approvals: `strict`" };
    }
    return { text: `Unknown operator command: \`${request.name}\`.` };
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
