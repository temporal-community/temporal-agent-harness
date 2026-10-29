// The scripted integration: a test-only agent whose inner loop plays a Script instead of calling a
// model, the twin of python/tests/conformance/scripted_agent.py. It publishes the events a model
// integration would (model interactions, reply deltas, tool requests), exactly as the script
// states them, and leaves everything else to the harness core, through the public authoring API
// only. A worker bundles this module as its workflows; worker.ts registers the `add` activity.

import { workflowInfo } from "@temporalio/workflow";
import { z } from "zod";
import {
  MidTurn,
  ToolApprovalPolicy,
  activityTool,
  defineAgent,
  handler,
  state,
  tool,
  type AgentWorkflowRunner,
  type AnyHarnessTool,
} from "@temporalio/agent-harness";
import { Script, ScriptReply, TOOL_NAMES } from "./script.ts";

export const SCRIPTED_AGENT = "ScriptedAgent";

export const add = activityTool({
  name: "add",
  description: "Add two integers.",
  inputSchema: z.object({ a: z.number().int(), b: z.number().int() }),
  outputSchema: z.number().int(),
  activityOptions: { startToCloseTimeout: "30 seconds" },
});

const echo = tool({
  name: "echo",
  description: "Return the text unchanged.",
  inputSchema: z.object({ text: z.string() }),
  outputSchema: z.string(),
  execute: async ({ text }) => text,
});

const fail = tool({
  name: "fail",
  description: "Fail with the message.",
  inputSchema: z.object({ message: z.string() }),
  outputSchema: z.string(),
  execute: async ({ message }) => {
    throw new Error(message);
  },
});

const gated = tool({
  name: "gated",
  description: "An action the approval policy gates.",
  inputSchema: z.object({ action: z.string() }),
  outputSchema: z.string(),
  execute: async ({ action }) => `done: ${action}`,
});

const askClient = tool({
  name: "ask_client",
  description: "Ask the attached client a question.",
  inputSchema: z.object({ question: z.string() }),
  outputSchema: z.string(),
});

const TOOLS: Record<(typeof TOOL_NAMES)[number], AnyHarnessTool> = {
  add,
  echo,
  fail,
  gated,
  ask_client: askClient,
};

const Entries = z.object({ values: z.record(z.string(), z.string()).default({}) });

const play = (description: string, midTurn: MidTurn) =>
  handler({ description, input: Script, output: ScriptReply, midTurn });

async function playScript(
  runner: AgentWorkflowRunner,
  entries: { mutate(recipe: (d: { values: Record<string, string> }) => void): void },
  subagents: Map<string, string>,
  script: Script,
): Promise<z.output<typeof ScriptReply>> {
  const reply: string[] = [];
  for (const step of script.steps) {
    switch (step.step) {
      case "model":
        runner.publish({ type: "model_interaction_started", model: step.model });
        for (const chunk of step.reply) runner.publish({ type: "reply_delta", text: chunk });
        reply.push(...step.reply);
        runner.publish({ type: "model_interaction_ended", model: step.model, usage: step.usage });
        break;
      case "tool":
        runner.publish({
          type: "tool_requested",
          tool_id: step.id,
          tool_name: step.name,
          tool_input: step.args,
        });
        // A failed call is the model's next input.
        await runner.runTool(step.id, TOOLS[step.name], step.args).catch(() => undefined);
        break;
      case "state_set":
        entries.mutate((d) => {
          d.values[step.key] = step.value;
        });
        break;
      case "state_delete":
        entries.mutate((d) => {
          // As Python's `del`: a missing key fails the step.
          if (!(step.key in d.values)) throw new Error(`'${step.key}'`);
          delete d.values[step.key];
        });
        break;
      case "subagent_start": {
        const info = workflowInfo();
        subagents.set(
          step.name,
          await runner.startSubagent("scripted", info.workflowType, info.taskQueue),
        );
        break;
      }
      case "subagent_send":
        // A failed turn is the model's next input.
        await runner
          .runSubagentTurn(subagents.get(step.name)!, step.handler, step.script)
          .catch(() => undefined);
        break;
      case "subagent_stop": {
        const handle = subagents.get(step.name)!;
        subagents.delete(step.name);
        await runner.stopSubagent(handle);
        break;
      }
      case "raise":
        throw new Error(step.message);
    }
  }
  return { text: reply.join("") };
}

export const ScriptedAgent = defineAgent({
  name: SCRIPTED_AGENT,
  approvalPolicyDefault: ToolApprovalPolicy.allowTools(["add", "echo", "fail", "ask_client"]),
  states: { entries: state(Entries) },
  handlers: {
    run_reject: play("Play the script; refused while a turn is open.", MidTurn.REJECT),
    run_enqueue: play("Play the script; queued behind an open turn.", MidTurn.ENQUEUE),
    run_accept: play("Play the script; joins an open turn.", MidTurn.ACCEPT),
  },
  create: ({ runner, state: { entries } }) => {
    const subagents = new Map<string, string>();
    const run = (script: Script) => playScript(runner, entries, subagents, script);
    return { run_reject: run, run_enqueue: run, run_accept: run };
  },
});
