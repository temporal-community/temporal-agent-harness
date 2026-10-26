// A hello-world OpenAI Agents SDK agent on the harness, with one tool call.
//
// A conversational agent that answers in plain text and can call a single tool (`get_weather`).
// The turn runs the model with `runStreamed(...)`, which routes model calls through the streaming
// activity so the harness observer can translate raw OpenAI events into the turn-stream events an
// attached client sees. The tool is a normal harness tool adapted onto the SDK with
// `asOpenAIAgentTool`, so the harness still owns approval and its `tool_start` / `tool_end` /
// `tool_error` events.
//
// Run it with the shared example stack (session-manager worker + server/UI); this agent is
// registered in `agents.toml` and driven by the packaged web app. See README.md.

import { Agent, type AgentInputItem } from "@openai/agents-core";
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
  asOpenAIAgentTool,
  runStreamed,
} from "@temporalio/agent-harness/ai-sdks/openai-agents-harness";

export const TASK_QUEUE = "openai-hello-ts";
export const DEFAULT_MODEL = "gpt-5.1";

const SYSTEM_INSTRUCTION = `\
You are a friendly assistant. Answer the user in brief, natural prose.

You have one tool, \`get_weather\`, which returns the current weather for a city. When the user
asks about the weather somewhere, call it (don't guess), then tell them the answer in a sentence
or two. For anything else, just reply directly.`;

export const getWeather = tool({
  name: "get_weather",
  description:
    'Return the current weather for a city. `city` is a plain city name, e.g. "Paris".',
  inputSchema: z.object({ city: z.string() }),
  outputSchema: z.string(),
  inherentlySafe: true,
  // Canned lookup: a hello-world, not a real weather service.
  execute: async ({ city }) => `It's 72°F and sunny in ${city}.`,
});

/** A one-tool conversational agent driven by the OpenAI Agents SDK. */
export const OpenAIHelloAgentTs = defineAgent({
  name: "OpenAIHelloAgentTs",
  // Hello-world stance: don't gate tool calls. A caller can tighten this per session via
  // AgentConfig.approval_policy.
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    ask: handler({
      description:
        "Chat with the assistant. Ask it anything; ask about the weather in a city and it\n" +
        "calls its `get_weather` tool and tells you what it found.",
      input: TextMessage,
      output: TextReply,
    }),
  },
  create: ({ runner }) => {
    // OpenAI conversation state, threaded across turns as the SDK's input-item list.
    let conversation: AgentInputItem[] = [];
    return {
      async ask({ text }) {
        const sdkAgent = new Agent({
          name: "Hello",
          instructions: SYSTEM_INSTRUCTION,
          model: DEFAULT_MODEL,
          tools: [asOpenAIAgentTool(runner, getWeather)],
        });
        const input: AgentInputItem[] = [...conversation, { role: "user", content: text }];

        // Iterate the streamed run's events to drive the turn to completion.
        const result = await runStreamed(runner, sdkAgent, input, {
          // Streaming leans on activity heartbeats to notice a stuck model call; keep the
          // heartbeat timeout well under the overall start-to-close.
          defaultModelParams: { startToCloseTimeout: "2 minutes", heartbeatTimeout: "30 seconds" },
        });
        for await (const _event of result) void _event;
        await result.completed;

        conversation = result.history;
        return { text: String(result.finalOutput) };
      },
    };
  },
});
