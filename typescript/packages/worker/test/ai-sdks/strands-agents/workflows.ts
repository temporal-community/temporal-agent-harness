// Probe agents for the Strands Agents integration tests: turns that run a harness Strands agent,
// its model calls served by the test's scripted models. The OpenAI Agents probe agents ride along,
// so one worker bundles both integrations' workflow code.

import { z } from "zod";
import { BeforeToolCallEvent } from "@strands-agents/sdk";
import {
  MidTurn,
  TextMessage,
  TextReply,
  ToolApprovalPolicy,
  defineAgent,
  handler,
  tool,
} from "@temporalio/agent-harness";
import { TemporalMCPClient } from "@temporalio/agent-harness/ai-sdks/strands-agents";
import {
  asStrandsTool,
  harnessStrandsAgent,
} from "@temporalio/agent-harness/ai-sdks/strands-agents-harness";
import { ECHO_MCP_SERVER, convertToCelsius } from "./tools.ts";

export { OpenAIWeatherAgent } from "../openai-agents/workflows.ts";

/** The model names the test's StrandsPlugin registers, one per probe agent. */
export const MODELS = {
  weather: "weather",
  ask: "ask",
  steered: "steered",
  interrupting: "interrupting",
  mcpApproved: "mcpApproved",
  mcpDenied: "mcpDenied",
} as const;

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
  description: "Chat with the assistant; sent mid-turn, the message steers it.",
  input: TextMessage,
  output: TextReply,
  midTurn: MidTurn.ACCEPT,
});

function strandsAgent(name: string, model: string, interruptBeforeTools = false) {
  return defineAgent({
    name,
    handlers: { ask },
    approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
    create: ({ runner }) => {
      const agent = harnessStrandsAgent(runner, {
        model,
        activityOptions: { startToCloseTimeout: "30 seconds" },
        printer: false,
        tools: [
          asStrandsTool(runner, getWeather),
          asStrandsTool(runner, convertToCelsius),
          asStrandsTool(runner, askUser),
        ],
      });
      // A Strands approval hook: it interrupts before each tool call to ask a person.
      if (interruptBeforeTools)
        agent.addHook(BeforeToolCallEvent, (event) => {
          event.interrupt({ name: "confirm-tool", reason: "Run this tool?" });
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
}

export const StrandsWeatherAgent = strandsAgent("StrandsWeatherAgent", MODELS.weather);
export const StrandsAskUserAgent = strandsAgent("StrandsAskUserAgent", MODELS.ask);
export const StrandsSteeredAgent = strandsAgent("StrandsSteeredAgent", MODELS.steered);
/** Calls an MCP server's tool under a policy that asks a person to approve every call. */
function mcpAgent(name: string, model: string) {
  return defineAgent({
    name,
    handlers: { ask },
    approvalPolicyDefault: ToolApprovalPolicy.alwaysRequireHumanApproval(),
    create: ({ runner }) => {
      const agent = harnessStrandsAgent(runner, {
        model,
        activityOptions: { startToCloseTimeout: "30 seconds" },
        printer: false,
        tools: [
          new TemporalMCPClient({
            server: ECHO_MCP_SERVER,
            activityOptions: { startToCloseTimeout: "30 seconds" },
          }),
        ],
      });
      return {
        async ask({ text }) {
          const result = await agent.invoke(text);
          return {
            text: result.lastMessage.content
              .flatMap((block) => (block.type === "textBlock" ? [block.text] : []))
              .join(""),
          };
        },
      };
    },
  });
}

export const StrandsMCPApprovedAgent = mcpAgent("StrandsMCPApprovedAgent", MODELS.mcpApproved);
export const StrandsMCPDeniedAgent = mcpAgent("StrandsMCPDeniedAgent", MODELS.mcpDenied);

export const StrandsInterruptingAgent = strandsAgent(
  "StrandsInterruptingAgent",
  MODELS.interrupting,
  true,
);
