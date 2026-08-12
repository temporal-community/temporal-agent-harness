import { describe, expect, it } from "vitest";
import type { AgentSseFrame, JsonRecord } from "$lib/api/types";
import { buildAgentGraph } from "./flowProjection";

function toolStart(
  toolId: string,
  toolName: string,
  toolInput: JsonRecord,
  resumeOffset: number
): AgentSseFrame {
  return {
    event: "tool_start",
    data: {
      type: "tool_start",
      agent_id: "agent-1",
      turn_id: "turn-1",
      turn_number: 1,
      timestamp: resumeOffset,
      resume_offset: resumeOffset,
      tool_id: toolId,
      tool_name: toolName,
      tool_input: toolInput
    }
  };
}

describe("buildAgentGraph tool layout", () => {
  it("keeps concurrent calls with the same tool name as distinct runtime nodes", () => {
    const graph = buildAgentGraph([
      toolStart("call-a", "lookup", { query: "alpha" }, 1),
      toolStart("call-b", "lookup", { query: "beta" }, 2)
    ]);

    const first = graph.nodes.find((node) => node.id === "tool:call-a");
    const second = graph.nodes.find((node) => node.id === "tool:call-b");

    expect(first?.data.toolId).toBe("call-a");
    expect(second?.data.toolId).toBe("call-b");
    expect(first?.data.flowGroup).toBe(second?.data.flowGroup);
    expect(first?.position).not.toEqual(second?.position);
  });

  it("lays Code Mode host calls inside their script container", () => {
    const graph = buildAgentGraph([
      toolStart("code-call", "execute_code", { script: "lookup('alpha')" }, 1),
      toolStart("host-a", "lookup", { query: "alpha" }, 2),
      toolStart("host-b", "lookup", { query: "beta" }, 3)
    ]);

    const parent = graph.nodes.find((node) => node.id === "tool:code-call");
    const firstChild = graph.nodes.find((node) => node.id === "tool:host-a");
    const secondChild = graph.nodes.find((node) => node.id === "tool:host-b");

    expect(parent?.data.codeMode).toBe(true);
    expect(parent?.data.size).toBe("container");
    expect(firstChild?.position.x).toBeGreaterThan(parent?.position.x ?? 0);
    expect(firstChild?.position.y).toBeGreaterThan(parent?.position.y ?? 0);
    expect(secondChild?.position.x).toBeGreaterThan(firstChild?.position.x ?? 0);
    expect(secondChild?.position.y).toBe(firstChild?.position.y);
  });
});
