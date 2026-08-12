import { describe, expect, it, vi } from "vitest";
import type { AgentApi } from "$lib/api/client";
import type { AgentSseFrame, Session } from "$lib/api/types";
import { AgentRunController, createAgentRunController } from "./agentRun.svelte";

const session: Session = {
  workflow_id: "root-workflow",
  created_at: 1,
  label: "Root session",
  agent_workflow_type: "QaAgent",
  is_message_queuing_enabled: true
};

describe("AgentRunController", () => {
  it("stores the canonical structured object for the first locally submitted user message", async () => {
    const originalMessage = {
      type: "example_action",
      payload: { opaque: true }
    };
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async () => ({
        workflow_id: session.workflow_id,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {},
      submitMessage: async () => ({
        turn_number: 1,
        turn_id: "turn-1",
        accepted_offset: 1,
        pending: false
      }),
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api, {
      messageText: () => "Extension request"
    });

    await controller.initialize();
    await controller.sendMessage(originalMessage as never);

    expect(controller.session?.initial_user_message).toBe(JSON.stringify(originalMessage));
  });

  it("retains the raw first streamed user-message string", async () => {
    const rawMessage = JSON.stringify({
      type: "example_action",
      payload: { opaque: true }
    });
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async () => ({
        workflow_id: session.workflow_id,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {
        yield {
          event: "turn_started",
          data: {
            type: "turn_started",
            agent_id: "agent-1",
            turn_id: "turn-1",
            turn_number: 1,
            timestamp: 1,
            resume_offset: 1,
            user_message: rawMessage
          }
        };
      },
      submitMessage: async () => ({
        turn_number: 1,
        turn_id: "turn-1",
        accepted_offset: 1,
        pending: false
      }),
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api, {
      messageText: () => "Extension request"
    });

    await controller.initialize();

    expect(controller.session?.initial_user_message).toBe(rawMessage);
  });

  it("projects an injected presentation adapter into transcript and replay-log views", () => {
    const controller = createAgentRunController({
      presentation: { messageText: () => "Extension request" }
    } as never);
    controller.session = session;
    controller.frames = [{
      event: "turn_started",
      data: {
        type: "turn_started",
        agent_id: "agent-1",
        turn_id: "turn-1",
        turn_number: 1,
        timestamp: 1,
        resume_offset: 1,
        user_message: JSON.stringify({
          type: "example_action",
          payload: { opaque: true }
        })
      }
    }] as AgentSseFrame[];
    controller.viewIndex = controller.total;

    expect(controller.chatTranscript).toContainEqual(
      expect.objectContaining({ kind: "user", text: "Extension request" })
    );
    expect(controller.replayLog.rows).toContainEqual(
      expect.objectContaining({ event: "turn_started", body: "Extension request" })
    );
  });

  it("rejects when workflow status cannot be checked before submit", async () => {
    const submitMessage = vi.fn();
    const failure = new TypeError("Failed to fetch workflow status");
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async () => { throw failure; },
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {},
      submitMessage,
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api);

    await expect(controller.sendMessage("Hello")).rejects.toBe(failure);

    expect(submitMessage).not.toHaveBeenCalled();
    expect(controller.connectionError).toBe(failure.message);
  });

  it("clears a stale submit transport error when replay confirms the root turn completed", async () => {
    let attachCount = 0;
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async () => ({
        workflow_id: session.workflow_id,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {
        attachCount += 1;
        if (attachCount === 1) {
          yield {
            event: "turn_started",
            data: {
              type: "turn_started",
              agent_id: "root-agent",
              turn_id: "turn-1",
              turn_number: 1,
              timestamp: 1,
              resume_offset: 1,
              user_message: "Earlier message"
            }
          };
        }
        if (attachCount === 2) {
          yield {
            event: "turn_started",
            data: {
              type: "turn_started",
              agent_id: "root-agent",
              turn_id: "turn-2",
              turn_number: 2,
              timestamp: 1,
              resume_offset: 2,
              user_message: "Hello"
            }
          };
          yield {
            event: "turn_end",
            data: {
              type: "turn_end",
              agent_id: "root-agent",
              turn_id: "turn-2",
              turn_number: 2,
              timestamp: 1,
              resume_offset: 1
            }
          };
        }
      },
      submitMessage: async () => {
        throw new Error("HTTP 502: upstream request timed out");
      },
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api);

    await controller.sendMessage("Hello");

    expect(controller.connectionError).toBeNull();
    expect(controller.frames).toContainEqual(expect.objectContaining({ event: "turn_end" }));
  });

  it("keeps a definitive submit rejection even when replay has a matching root turn", async () => {
    let attachCount = 0;
    const rejection = Object.assign(new Error("HTTP 422: stale draft"), { status: 422 });
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async () => ({
        workflow_id: session.workflow_id,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {
        attachCount += 1;
        if (attachCount !== 2) return;
        yield {
          event: "turn_started",
          data: {
            type: "turn_started",
            agent_id: "root-agent",
            turn_id: "turn-1",
            turn_number: 1,
            timestamp: 1,
            resume_offset: 1,
            user_message: "Hello"
          }
        };
        yield {
          event: "turn_end",
          data: {
            type: "turn_end",
            agent_id: "root-agent",
            turn_id: "turn-1",
            turn_number: 1,
            timestamp: 2,
            resume_offset: 2
          }
        };
      },
      submitMessage: async () => { throw rejection; },
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api);

    await expect(controller.sendMessage("Hello")).rejects.toBe(rejection);

    expect(controller.connectionError).toBe(rejection.message);
  });

  it("does not reconcile an ambiguous structured submit against a different payload", async () => {
    let attachCount = 0;
    const submitted = {
      type: "operator_request",
      payload: { request_id: "request-1", revision: "A" }
    } as const;
    const concurrent = {
      type: "operator_request",
      payload: { request_id: "request-1", revision: "B" }
    } as const;
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async () => ({
        workflow_id: session.workflow_id,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {
        attachCount += 1;
        if (attachCount !== 2) return;
        yield {
          event: "turn_started",
          data: {
            type: "turn_started",
            agent_id: "root-agent",
            turn_id: "turn-1",
            turn_number: 1,
            timestamp: 1,
            resume_offset: 1,
            user_message: JSON.stringify(concurrent)
          }
        };
        yield {
          event: "turn_end",
          data: {
            type: "turn_end",
            agent_id: "root-agent",
            turn_id: "turn-1",
            turn_number: 1,
            timestamp: 2,
            resume_offset: 2
          }
        };
      },
      submitMessage: async () => { throw new Error("HTTP 502: uncertain submit"); },
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api);

    await expect(controller.sendMessage(submitted as never)).rejects.toThrow(
      "HTTP 502: uncertain submit"
    );
  });

  it("keeps a failed submit authoritative when a child ends the same turn on the root stream", async () => {
    let attachCount = 0;
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async () => ({
        workflow_id: session.workflow_id,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {
        attachCount += 1;
        if (attachCount === 1) {
          yield {
            event: "turn_started",
            data: {
              type: "turn_started",
              agent_id: "root-agent",
              turn_id: "turn-1",
              turn_number: 1,
              timestamp: 1,
              resume_offset: 1,
              user_message: "Earlier message"
            }
          };
          yield {
            event: "subagent_started",
            data: {
              type: "subagent_started",
              agent_id: "root-agent",
              turn_id: "turn-1",
              turn_number: 1,
              timestamp: 1,
              resume_offset: 2,
              subagent_id: "child-agent",
              agent_key: "child",
              workflow_id: "child-workflow"
            }
          };
        }
        if (attachCount === 2) {
          yield {
            event: "turn_end",
            data: {
              type: "turn_end",
              agent_id: "child-agent",
              turn_id: "turn-2",
              turn_number: 2,
              timestamp: 2,
              resume_offset: 3
            }
          };
        }
      },
      submitMessage: async () => {
        throw new Error("HTTP 502: upstream request timed out");
      },
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api);

    await expect(controller.sendMessage("Hello")).rejects.toThrow(
      "HTTP 502: upstream request timed out"
    );

    expect(controller.connectionError).toBe("HTTP 502: upstream request timed out");
    expect(controller.replayTimeline.at(-1)?.role).toBe("subagent");
    expect(controller.expectedTurn).toBe(2);
  });

  it("keeps a submit transport error when an unknown child stream turn ends during replay", async () => {
    let releaseChildTurnEnd: () => void;
    const childTurnEndReady = new Promise<void>((resolve) => {
      releaseChildTurnEnd = resolve;
    });
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async (workflowId) => ({
        workflow_id: workflowId,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (workflowId): AsyncIterable<AgentSseFrame> {
        if (workflowId === session.workflow_id) {
          yield {
            event: "subagent_started",
            data: {
              type: "subagent_started",
              agent_id: "root-agent",
              turn_id: "turn-1",
              turn_number: 1,
              timestamp: 1,
              resume_offset: 1,
              subagent_id: "child-agent",
              agent_key: "child",
              workflow_id: "child-workflow"
            }
          };
          return;
        }
        if (workflowId === "child-workflow") {
          await childTurnEndReady;
          yield {
            event: "turn_end",
            data: {
              type: "turn_end",
              agent_id: "unknown-child-agent",
              turn_id: "turn-1",
              turn_number: 1,
              timestamp: 1,
              resume_offset: 1
            }
          };
        }
      },
      submitMessage: async () => {
        throw new Error("HTTP 502: upstream request timed out");
      },
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api);

    await controller.initialize();
    await controller.executeOperatorCommand("inspect", null, "child-workflow");
    await expect(controller.sendMessage("Hello")).rejects.toThrow(
      "HTTP 502: upstream request timed out"
    );
    releaseChildTurnEnd!();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(controller.frames).toContainEqual(expect.objectContaining({ event: "turn_end" }));
    expect(controller.connectionError).toBe("HTTP 502: upstream request timed out");
  });

  it("keeps a stream connection error when a root turn ends", async () => {
    let attachCount = 0;
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async (workflowId) => ({
        workflow_id: workflowId,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {
        attachCount += 1;
        if (attachCount === 2) {
          throw new Error("Root stream disconnected");
        }
        if (attachCount === 3) {
          yield {
            event: "turn_end",
            data: {
              type: "turn_end",
              agent_id: "root-agent",
              turn_id: "turn-1",
              turn_number: 1,
              timestamp: 1,
              resume_offset: 1
            }
          };
        }
      },
      submitMessage: async () => ({
        turn_number: 1,
        turn_id: "turn-1",
        accepted_offset: 1,
        pending: false
      }),
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api);

    await controller.initialize();
    await controller.executeOperatorCommand("inspect");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(controller.connectionError).toBe("Root stream disconnected");

    await controller.attach();

    expect(controller.connectionError).toBe("Root stream disconnected");
  });

  it("keeps a later stream error with the same text when the failed submit turn ends", async () => {
    let attachCount = 0;
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async (workflowId) => ({
        workflow_id: workflowId,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {
        attachCount += 1;
        if (attachCount === 1) {
          yield {
            event: "turn_started",
            data: {
              type: "turn_started",
              agent_id: "root-agent",
              turn_id: "turn-1",
              turn_number: 1,
              timestamp: 1,
              resume_offset: 1,
              user_message: "Earlier message"
            }
          };
        }
        if (attachCount === 3) {
          throw new Error("Internal Server Error");
        }
        if (attachCount === 4) {
          yield {
            event: "turn_end",
            data: {
              type: "turn_end",
              agent_id: "root-agent",
              turn_id: "turn-2",
              turn_number: 2,
              timestamp: 2,
              resume_offset: 2
            }
          };
        }
      },
      submitMessage: async () => {
        throw new Error("Internal Server Error");
      },
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api);

    await expect(controller.sendMessage("Hello")).rejects.toThrow(
      "Internal Server Error"
    );
    await controller.executeOperatorCommand("inspect");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(controller.connectionError).toBe("Internal Server Error");

    await controller.attach();

    expect(controller.connectionError).toBe("Internal Server Error");
  });

  it("follows a newly initialized open session when its stream is empty", async () => {
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [],
      createSession: async () => session,
      workflowStatus: async () => ({
        workflow_id: session.workflow_id,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {},
      submitMessage: async () => ({
        turn_number: 1,
        turn_id: "turn-1",
        accepted_offset: 1,
        pending: false
      }),
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api);

    await controller.initialize();

    expect(controller.total).toBe(0);
    expect(controller.viewIndex).toBe(0);
    expect(controller.following).toBe(true);
  });

  it("follows a new empty session after resetting from historical replay", async () => {
    const newSession: Session = {
      ...session,
      workflow_id: "new-root-workflow",
      label: "New root session"
    };
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => newSession,
      workflowStatus: async (workflowId) => ({
        workflow_id: workflowId,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (workflowId): AsyncIterable<AgentSseFrame> {
        if (workflowId === session.workflow_id) {
          yield {
            event: "turn_started",
            data: {
              type: "turn_started",
              agent_id: "root-agent",
              turn_id: "turn-1",
              turn_number: 1,
              timestamp: 1,
              resume_offset: 1,
              user_message: "Earlier message"
            }
          };
        }
      },
      submitMessage: async () => ({
        turn_number: 1,
        turn_id: "turn-1",
        accepted_offset: 1,
        pending: false
      }),
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve: async () => ({ tool_id: "unused", accepted: true })
    };
    const controller = new AgentRunController(api);

    await controller.initialize();
    controller.goTo(0);
    expect(controller.following).toBe(false);

    await controller.startNewSession();

    expect(controller.total).toBe(0);
    expect(controller.viewIndex).toBe(0);
    expect(controller.following).toBe(true);
  });

  it("routes a subagent approval to the workflow that requested it", async () => {
    const approve = vi.fn(async () => ({ tool_id: "shared-tool", accepted: true as const }));
    const api: AgentApi = {
      listAgents: async () => ({
        agents: [{
          key: "qa",
          workflow_type: "QaAgent",
          task_queue: "qa",
          label: "Q&A Agent",
          description: "Test agent"
        }]
      }),
      listSessions: async () => [session],
      createSession: async () => session,
      workflowStatus: async (workflowId) => ({
        workflow_id: workflowId,
        execution_status: "Running",
        closed: false
      }),
      acceptedMessageTypes: async () => ({ accepts_text: true, models: [] }),
      agentInterface: async () => [],
      operatorInterface: async () => [],
      executeOperatorCommand: async () => ({ text: "" }),
      attach: async function* (): AsyncIterable<AgentSseFrame> {
        yield {
          event: "subagent_started",
          data: {
            type: "subagent_started",
            agent_id: "root-agent",
            turn_id: "turn-1",
            turn_number: 1,
            timestamp: 1,
            resume_offset: 1,
            subagent_id: "child-agent",
            agent_key: "child",
            workflow_id: "child-workflow"
          }
        };
      },
      submitMessage: async () => ({
        turn_number: 1,
        turn_id: "turn-1",
        accepted_offset: 1,
        pending: false
      }),
      chat: async function* (): AsyncIterable<AgentSseFrame> {},
      approve
    };
    const controller = new AgentRunController(api);

    await controller.initialize();
    await controller.approveTool("child-workflow", "shared-tool", true);

    expect(approve).toHaveBeenCalledWith(expect.objectContaining({
      session_id: "child-workflow",
      tool_id: "shared-tool"
    }));
  });

  it("projects chat transcript items only through the replay cutoff", () => {
    const controller = new AgentRunController({} as AgentApi);
    controller.session = session;
    controller.frames = [
      {
        event: "turn_started",
        data: {
          type: "turn_started",
          agent_id: "root-agent",
          turn_id: "turn-1",
          turn_number: 1,
          timestamp: 1,
          resume_offset: 1,
            user_message: "Prepare response"
        }
      },
      {
        event: "reply",
        data: {
          type: "reply",
          agent_id: "root-agent",
          turn_id: "turn-1",
          turn_number: 1,
          timestamp: 2,
          resume_offset: 2,
          text: "Draft is not ready yet.",
          output: null
        }
      },
      {
        event: "reply",
        data: {
          type: "reply",
          agent_id: "root-agent",
          turn_id: "turn-1",
          turn_number: 1,
          timestamp: 3,
          resume_offset: 3,
          text: "Future response",
          output: { response: { revision: "future" } }
        }
      }
    ] as AgentSseFrame[];
    controller.goTo(2);

    expect(controller.visibleChatTranscript).toEqual([
      expect.objectContaining({ kind: "user", text: "Prepare response" }),
      expect.objectContaining({ kind: "agent", text: "Draft is not ready yet." })
    ]);
    expect(controller.visibleChatTranscript).not.toContainEqual(
      expect.objectContaining({ kind: "agent", text: "Future response" })
    );
  });
});
