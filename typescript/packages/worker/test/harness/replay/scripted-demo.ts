// A model-free agent whose recorded histories cover the harness's mechanics end to end: a scripted
// "model" activity streams its reply and picks tools by keyword, and the handler runs them as a
// real SDK loop would. "weather" runs an inline tool, "search"/"book" activity tools (booking asks
// for approval), "file" a callback tool, "code" a Code Mode script over search and book, and
// "echo" starts, messages and stops a subagent.

import { proxyActivities, workflowInfo } from "@temporalio/workflow";
import {
  MidTurn,
  TextMessage,
  TextReply,
  ToolApprovalPolicy,
  activityTool,
  codeModeTool,
  defineAgent,
  handler,
  state,
  subagentToolset,
  tool,
  type AnyHarnessTool,
  type TurnStreamContext,
} from "@temporalio/agent-harness";
import { z } from "zod";

/** Tool rounds in one ask. */
const MAX_ROUNDS = 6;

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ModelStep {
  text: string;
  toolCalls: ToolCall[];
}

export interface ScriptedModelInput {
  prompt: string;
  round: number;
  results: { call: ToolCall; output: string }[];
  streamContext: TurnStreamContext;
}

export interface ScriptedModelActivities {
  scripted_model(input: ScriptedModelInput): Promise<ModelStep>;
}

export const Flight = z
  .object({ flight_id: z.string(), price_usd: z.number() })
  .meta({ title: "Flight" });

export const getWeather = tool({
  name: "get_weather",
  description: "The current weather for a city.",
  inputSchema: z.object({ city: z.string() }),
  outputSchema: z.string(),
  inherentlySafe: true,
  execute: async ({ city }) => `It's 72°F and sunny in ${city}.`,
});

export const searchFlights = activityTool({
  name: "search_flights",
  description: "Search flights between two airports.",
  inputSchema: z.object({ origin: z.string(), destination: z.string() }),
  outputSchema: z.array(Flight),
  inherentlySafe: true,
});

export const bookFlight = activityTool({
  name: "book_flight",
  description: "Book a flight by id; returns a confirmation code.",
  inputSchema: z.object({ flight_id: z.string() }),
  outputSchema: z.string(),
});

export const readLocalFile = tool({
  name: "read_local_file",
  description: "Read a file on the user's machine.",
  inputSchema: z.object({ path: z.string() }),
  outputSchema: z.object({ content: z.string() }),
  inherentlySafe: true,
});

export const EchoAgent = defineAgent({
  name: "EchoAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    echo: handler({
      description: "Echo the text back, upper-cased.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
  },
  create: () => ({ echo: async ({ text }) => ({ text: text.toUpperCase() }) }),
});

const Notes = z.object({ notes: z.array(z.string()).default([]) }).meta({ title: "Notes" });

export const ScriptedDemoAgent = defineAgent({
  name: "ScriptedDemoAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowInherentlySafe(),
  states: { notes: state(Notes) },
  handlers: {
    ask: handler({
      description: "Chat with the scripted demo agent.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
  },
  create: ({ runner, state: { notes } }) => {
    const tools = new Map<string, AnyHarnessTool>(
      [
        getWeather,
        searchFlights,
        bookFlight,
        readLocalFile,
        codeModeTool([searchFlights, bookFlight], { name: "run_travel_code" }),
        ...subagentToolset(EchoAgent, { key: "echo", taskQueue: workflowInfo().taskQueue }),
      ].map((t) => [t.name, t as AnyHarnessTool]),
    );
    const { scripted_model } = proxyActivities<ScriptedModelActivities>({
      startToCloseTimeout: "30 seconds",
    });

    return {
      async ask({ text }) {
        let results: { call: ToolCall; output: string }[] = [];
        for (let round = 0; round < MAX_ROUNDS; round++) {
          const step = await scripted_model({
            prompt: text,
            round,
            results,
            streamContext: runner.currentStreamContext!,
          });
          if (step.toolCalls.length === 0) {
            notes.mutate((d) => {
              d.notes.push(text);
            });
            return { text: step.text };
          }
          results = await Promise.all(
            step.toolCalls.map(async (call) => {
              try {
                const out = await runner.runTool(call.id, tools.get(call.name)!, call.arguments);
                return { call, output: typeof out === "string" ? out : JSON.stringify(out) };
              } catch (e) {
                return { call, output: `error: ${e instanceof Error ? e.message : String(e)}` };
              }
            }),
          );
        }
        return { text: "I ran out of steps." };
      },
    };
  },
});
