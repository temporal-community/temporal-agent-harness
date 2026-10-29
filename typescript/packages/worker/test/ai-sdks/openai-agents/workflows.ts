// Probe agents for the OpenAI Agents SDK integration tests: a turn that runs an Agents SDK agent
// with one harness tool or one MCP server, its model calls served by the test's scripted provider.

import { Agent } from "@openai/agents-core";
import { z } from "zod";
import {
  TextMessage,
  TextReply,
  ToolApprovalPolicy,
  defineAgent,
  handler,
  tool,
} from "@temporalio/agent-harness";
import {
  asOpenAIAgentTools,
  runStreamed,
  statelessMcpServer,
} from "@temporalio/agent-harness/ai-sdks/openai-agents-harness";
import {
  PROBE_MCP_SERVER,
  SCRIPTED_ASK_MODEL,
  SCRIPTED_MCP_ERROR_MODEL,
  SCRIPTED_MCP_MODEL,
  SCRIPTED_MODEL,
  WORKFLOW_MODEL_TIMEOUT,
} from "./scripted-model.ts";
// The same tool as an activity tool; the test's worker registers its body.
import { getWeatherActivity } from "./tools.ts";

const getWeather = tool({
  name: "get_weather",
  description: "Return the current weather for a city.",
  inputSchema: z.object({ city: z.string() }),
  outputSchema: z.string(),
  execute: async ({ city }) => `It's 72F and sunny in ${city}.`,
});

/** A callback tool: the attached client answers it. */
const askUser = tool({
  name: "ask_user",
  description: "Ask the user a question and wait for their answer.",
  inputSchema: z.object({ question: z.string() }),
  outputSchema: z.string(),
});

const ask = handler({
  description: "Chat with the assistant.",
  input: TextMessage,
  output: TextReply,
});

function weatherAgent(
  name: string,
  approvalPolicyDefault: ToolApprovalPolicy,
  harnessTool: typeof getWeather | typeof getWeatherActivity | typeof askUser = getWeather,
  model = SCRIPTED_MODEL,
) {
  return defineAgent({
    name,
    handlers: { ask },
    approvalPolicyDefault,
    create: ({ runner }) => ({
      async ask({ text }) {
        const agent = new Agent({
          name: "Weather",
          instructions: "Answer weather questions with get_weather.",
          model,
          tools: asOpenAIAgentTools(runner, [harnessTool]),
        });
        const result = await runStreamed(runner, agent, text, {
          defaultModelParams: { startToCloseTimeout: WORKFLOW_MODEL_TIMEOUT },
        });
        for await (const _ of result) void _;
        await result.completed;
        return { text: String(result.finalOutput) };
      },
    }),
  });
}

/** Its tool runs ungated. */
export const OpenAIWeatherAgent = weatherAgent(
  "OpenAIWeatherAgent",
  ToolApprovalPolicy.dangerouslySkipAll(),
);

/** Its tool waits for a human's approval. */
export const OpenAIGatedWeatherAgent = weatherAgent(
  "OpenAIGatedWeatherAgent",
  ToolApprovalPolicy.alwaysRequireHumanApproval(),
);

/** Its tool is an activity tool. */
export const OpenAIActivityWeatherAgent = weatherAgent(
  "OpenAIActivityWeatherAgent",
  ToolApprovalPolicy.dangerouslySkipAll(),
  getWeatherActivity,
);

/** Its tool is a callback tool, which asks the user. */
export const OpenAIAskUserAgent = weatherAgent(
  "OpenAIAskUserAgent",
  ToolApprovalPolicy.dangerouslySkipAll(),
  askUser,
  SCRIPTED_ASK_MODEL,
);

function mcpAgent(name: string, model: string, approvalPolicyDefault: ToolApprovalPolicy) {
  return defineAgent({
    name,
    handlers: { ask },
    approvalPolicyDefault,
    create: ({ runner }) => ({
      async ask({ text }) {
        const agent = new Agent({
          name: "Lookup",
          instructions: "Look things up with the MCP server.",
          model,
          mcpServers: [statelessMcpServer(PROBE_MCP_SERVER, { runner })],
        });
        const result = await runStreamed(runner, agent, text);
        for await (const _ of result) void _;
        await result.completed;
        return { text: String(result.finalOutput) };
      },
    }),
  });
}

/** Its one MCP server's tools wait for a human's approval. */
export const OpenAIMcpAgent = mcpAgent(
  "OpenAIMcpAgent",
  SCRIPTED_MCP_MODEL,
  ToolApprovalPolicy.alwaysRequireHumanApproval(),
);

/** Looks up a term its MCP server reports as an error. */
export const OpenAIFailingMcpAgent = mcpAgent(
  "OpenAIFailingMcpAgent",
  SCRIPTED_MCP_ERROR_MODEL,
  ToolApprovalPolicy.dangerouslySkipAll(),
);
