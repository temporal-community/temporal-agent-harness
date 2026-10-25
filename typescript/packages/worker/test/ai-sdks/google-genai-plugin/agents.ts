// Probe agents running the Gemini Interactions tool loop, for tool-loop.test.ts: the workflow
// holds the conversation, the model calls run in the plugin's activity, and each function call
// runs through runner.runTool.

import { isCancellation } from "@temporalio/workflow";
import { z } from "zod";
import {
  TextMessage,
  TextReply,
  ToolApprovalPolicy,
  activityTool,
  defineAgent,
  handler,
  tool,
  type AgentWorkflowRunner,
} from "@temporalio/agent-harness";
import {
  InteractionConversation,
  functionParam,
  googleGenaiClient,
  type FunctionCallStep,
  type FunctionResultStepParam,
} from "@temporalio/agent-harness/ai-sdks/google-genai-plugin";

export const MODEL = "gemini-test-model";

export const getWeather = activityTool({
  name: "get_weather",
  description: "The current weather in a city.",
  inputSchema: z.object({ city: z.string() }),
  outputSchema: z.string(),
});

/** A callback tool: the attached client answers it. */
export const askUser = tool({
  name: "ask_user",
  description: "Ask the user a question and wait for their answer.",
  inputSchema: z.object({ question: z.string() }),
  outputSchema: z.string(),
});

type LoopTool = typeof getWeather | typeof askUser;

async function runCall(
  runner: AgentWorkflowRunner,
  tools: readonly LoopTool[],
  call: FunctionCallStep,
): Promise<FunctionResultStepParam> {
  const base: FunctionResultStepParam = {
    type: "function_result",
    call_id: call.id,
    name: call.name,
    result: "",
  };
  if (call.signature) base.signature = call.signature;
  try {
    const tool = tools.find((t) => t.name === call.name);
    if (tool === undefined) throw new Error(`unknown tool: '${call.name}'`);
    return { ...base, result: await runner.runTool(call.id, tool, call.arguments) };
  } catch (e) {
    if (isCancellation(e)) throw e;
    return { ...base, result: e instanceof Error ? e.message : String(e), is_error: true };
  }
}

function toolLoopAgent(name: string, tools: readonly LoopTool[]) {
  return defineAgent({
    name,
    approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
    handlers: {
      ask: handler({ description: "Ask the model.", input: TextMessage, output: TextReply }),
    },
    create: ({ runner }) => {
      const gemini = googleGenaiClient({ runner });
      const conversation = new InteractionConversation();
      return {
        async ask({ text }) {
          conversation.addUserText(text);
          while (true) {
            const stream = await gemini.interactions.create({
              model: MODEL,
              input: conversation.steps,
              tools: tools.map(functionParam),
              stream: true,
            });
            const reply = await conversation.readReply(stream);
            if (reply.functionCalls.length === 0) return { text: reply.text };
            conversation.addFunctionResults(
              await Promise.all(reply.functionCalls.map((call) => runCall(runner, tools, call))),
            );
          }
        },
      };
    },
  });
}

/** Its one tool is an activity tool. */
export const GeminiToolLoopAgent = toolLoopAgent("GeminiToolLoopAgent", [getWeather]);

/** Its one tool is a callback tool. */
export const GeminiAskUserAgent = toolLoopAgent("GeminiAskUserAgent", [askUser]);
