// A hello-world Strands Agents agent on the harness, with an inline, an activity and a callback
// tool.
//
// A conversational agent that answers in plain text. It is a Strands agent built with
// `harnessStrandsAgent`, so each model call runs in the Strands plugin's model activity, where the
// harness translates raw Strands stream events into the turn-stream events an attached client
// sees. Its tools cover each kind a harness Strands agent accepts:
//
//   * `get_weather`: an inline workflow tool (`tool` with `execute`).
//   * `get_local_time`: an activity tool (`activityTool`): reading the clock is nondeterministic,
//     so it runs on the worker as a durable activity (its body is in worker.ts).
//   * `ask_user`: a callback tool (`tool` without `execute`): the call waits in the workflow until
//     the person in the console answers.
//
// A message sent while the agent is working steers it: `ask` joins the open turn and queues the
// text with `runner.steer`, and the harness hands it to the model with the next tool result.
//
// Run it with the shared example stack (session-manager worker + server/UI); this agent is
// registered in `agents.toml` and driven by the packaged web app. See README.md.

import { z } from "zod";
import {
  MidTurn,
  TextMessage,
  TextReply,
  ToolApprovalPolicy,
  activityTool,
  defineAgent,
  handler,
  tool,
} from "@temporalio/agent-harness";
import {
  asStrandsTool,
  harnessStrandsAgent,
} from "@temporalio/agent-harness/ai-sdks/strands-agents-harness";

export const TASK_QUEUE = "strands-hello-ts";
/** The name the worker's StrandsPlugin registers its model under (see worker.ts). */
export const MODEL = "openai";

const SYSTEM_PROMPT = `\
You are a friendly assistant. Answer the user in brief, natural prose.

Your tools:
- \`get_weather\`: the current weather for a city. Call it rather than guess.
- \`get_local_time\`: the current local time in an IANA time zone, e.g. "Europe/Paris".
- \`ask_user\`: ask the user a question when you need something only they know.`;

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

export const getLocalTime = activityTool({
  name: "get_local_time",
  description: 'Return the current local time in an IANA time zone, e.g. "Europe/Paris".',
  inputSchema: z.object({ time_zone: z.string() }),
  outputSchema: z.string(),
  inherentlySafe: true,
});

export const askUser = tool({
  name: "ask_user",
  description: "Ask the user a question and return their answer.",
  inputSchema: z.object({ question: z.string() }),
  outputSchema: z.string(),
  inherentlySafe: true,
});

/** A conversational agent driven by Strands Agents, with every kind of harness Strands tool. */
export const StrandsHelloAgentTs = defineAgent({
  name: "StrandsHelloAgentTs",
  // Every tool here is inherently safe, so it runs without waiting for a person's approval.
  approvalPolicyDefault: ToolApprovalPolicy.allowInherentlySafe(),
  handlers: {
    ask: handler({
      description:
        "Chat with the assistant. Ask about the weather or the time somewhere; it may ask you a\n" +
        "question back. Sent while it is working, the message steers it instead: the text\n" +
        "reaches the model with the next tool result.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ACCEPT,
    }),
  },
  create: ({ runner }) => {
    // Strands keeps the conversation in the agent, which lives as long as the workflow.
    const agent = harnessStrandsAgent(runner, {
      model: MODEL,
      activityOptions: { startToCloseTimeout: "2 minutes" },
      systemPrompt: SYSTEM_PROMPT,
      tools: [
        asStrandsTool(runner, getWeather),
        asStrandsTool(runner, getLocalTime),
        asStrandsTool(runner, askUser),
      ],
      // Replies reach the console through the turn stream, not Strands' stdout printer.
      printer: false,
    });
    return {
      async ask({ text }, ctx) {
        if (ctx.joined_turn) {
          runner.steer(text);
          return { text: "" };
        }
        const result = await agent.invoke(text);
        // The reply's text blocks: a Strands AgentResult's toString() also prints its reasoning.
        return {
          text: result.lastMessage.content
            .flatMap((block) => (block.type === "textBlock" ? [block.text] : []))
            .join(""),
        };
      },
    };
  },
});
