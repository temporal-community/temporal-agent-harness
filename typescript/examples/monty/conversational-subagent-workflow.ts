// Conversational Monty agent that drives the script-runner as a SUBAGENT (Python's
// conversational_subagent_workflow.py).
//
// The subagent-flavoured twin of MontyChatAgentTs (conversational-workflow.ts). Both put a *model
// in the loop*: the user chats in plain text, the model converses to gather what it needs, then
// writes its own Python script and runs it. The conversational front end, the Gemini Interactions
// tool-calling loop, is the same shape.
//
// The ONE difference is *where the script runs*. MontyChatAgentTs runs each model-authored script
// inline, via a `run_travel_code` Code Mode tool held on the workflow itself. This agent instead
// drives the barebones MontyDynamicAgentTs (workflow.ts), whose sole handler,
// `run_script(RunScript) -> TextReply`, runs a script in its own Code Mode over the same travel
// tools, as a **subagent**. The script-runner is wired with `subagentToolset`, which generates
// three model-facing tools: `start_monty` (start an instance, returns a short handle),
// `monty_run_script` (send a script to that instance and get its reply), and `stop_monty` (shut it
// down). So `monty_run_script` is the subagent counterpart of the inline agent's
// `run_travel_code` tool: same capability, now across a real parent→subagent boundary.
//
// Approval stance: this agent allows only `ask_user` (like the inline agent), so it **gates the
// subagent tools**: every `start_monty` / `monty_run_script` / `stop_monty` call escalates to a human. The
// script's host calls (search/book flights & hotels) run *inside the child*, which keeps its own
// `dangerouslySkipAll` policy, so (unlike the inline agent) those host calls are NOT gated in the
// parent.

import {
  MidTurn,
  TextMessage,
  TextReply,
  ToolApprovalPolicy,
  defineAgent,
  handler,
  subagentToolset,
} from "@temporalio/agent-harness";
import {
  InteractionConversation,
  functionParam,
  googleGenaiClient,
} from "@temporalio/agent-harness/ai-sdks/google-genai-plugin";
import {
  SET_MODEL_DESCRIPTION,
  SUPPORTED_MODELS,
  SetModel,
  runFunctionCall,
} from "./conversational-workflow.ts";
import { askUser } from "./ask-user.ts";
import { MontyDynamicAgentTs, TASK_QUEUE } from "./workflow.ts";

const DEFAULT_MODEL = SUPPORTED_MODELS[0];

// The namespace for the wired script-runner subagent. Tool names are derived from it:
// start_monty / monty_run_script / stop_monty.
const SUBAGENT_KEY = "monty";

const SYSTEM_INSTRUCTION = `\
You are a friendly travel-booking assistant. You help users search and book flights and \
hotels and assemble trip itineraries. You don't have these abilities directly — instead you \
write small async Python scripts and run them on a dedicated script-runner subagent, using \
these tools:
- \`start_${SUBAGENT_KEY}\`: start a script-runner and get back a short \`subagent\` handle. Call \
this ONCE at the start of the conversation, then reuse the same handle for every script.
- \`${SUBAGENT_KEY}_run_script\`: send a script to the running script-runner. Pass the handle from \
\`start_${SUBAGENT_KEY}\` as \`subagent\`, and the script in \`message\` (a RunScript object with a \
\`script\` field). Its description documents the full sandbox contract — the async script \
structure, the concurrency rules, and the exact host-function signatures and result shapes; \
follow it. The reply carries the script's printed output and final value.

How to behave:
- Converse naturally. When you're missing something essential (origin/destination, dates, \
traveler name) or need the user to choose between options, ask them with the \`ask_user\` tool \
and carry on with their answers in the same turn. Ask ONE question per \`ask_user\` call; to ask \
several things, make one call per question in the same reply. Don't interrogate; make \
reasonable assumptions and state them.
- Before running your first script, call \`start_${SUBAGENT_KEY}\` to get a handle. Keep using \
that one handle for the rest of the conversation; don't start a new script-runner per script.
- When you have enough to make progress, WRITE A SCRIPT and run it with \
\`${SUBAGENT_KEY}_run_script\`. Run independent host calls concurrently with \`asyncio.gather\`; keep \
each script focused (search, or book, or summarize) so you can react to results.
- After a tool result, read it and reply to the user in plain, friendly prose — summarize \
options, prices, confirmations. You may run more scripts in follow-up turns.
- Never invent flight/hotel ids or confirmation codes — only use ones returned by a script.`;

export const MontyChatSubagentAgentTs = defineAgent({
  name: "MontyChatSubagentAgentTs",
  // Gate the subagent tools: every start_monty / monty_run_script / stop_monty call escalates to
  // a human (same stance as the inline MontyChatAgentTs). The script's host calls run inside the
  // child, which has its own dangerouslySkipAll policy. `ask_user` alone is allowed: asking the
  // user a question needs no approval.
  approvalPolicyDefault: ToolApprovalPolicy.allowTools([askUser.name]),
  handlers: {
    ask: handler({
      description: `Chat with the travel assistant. Describe the trip you want (flights, hotels,
dates, traveler name) in plain text; the assistant converses, writes Python scripts,
and runs them against a simulated travel backend via a script-runner subagent, then
replies with the results.`,
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
    // See MontyChatAgentTs's set_model for why this is ACCEPT and not model-callable.
    set_model: handler({
      description: SET_MODEL_DESCRIPTION,
      input: SetModel,
      output: TextReply,
      midTurn: MidTurn.ACCEPT,
      modelCallable: false,
    }),
  },
  create: ({ runner }) => {
    let model: string = DEFAULT_MODEL;
    // The whole conversation, sent as the input of every model call (see InteractionConversation
    // for why it is not chained by interaction id).
    const conversation = new InteractionConversation();
    // The model-facing tools: drive the barebones MontyDynamicAgentTs script-runner as a
    // subagent. Built statically from its handlers; no child is started here. Yields start_monty /
    // monty_run_script / stop_monty. Plus ask_user, to ask the user something mid-turn.
    const modelTools = [
      ...subagentToolset(MontyDynamicAgentTs, { key: SUBAGENT_KEY, taskQueue: TASK_QUEUE }),
      askUser,
    ];
    const toolsByName = new Map(modelTools.map((t) => [t.name, t]));
    // Same Temporal-aware client as the inline agent; the runner is wired in so reply text
    // streams to the workflow stream as it is generated.
    const gemini = googleGenaiClient({
      activityConfig: { startToCloseTimeout: "3 minutes" },
      runner,
    });

    /**
     * Run one conversational turn: stream the model, dispatch any subagent tool calls, feed
     * results back, and loop until the model replies with no further calls. The generated
     * `monty_run_script` returns a TextReply, which goes back to the model as JSON.
     */
    async function handleChatTurn(userText: string): Promise<string> {
      const tools = modelTools
        .filter((t) => t.name !== `stop_${SUBAGENT_KEY}`)
        .map(functionParam);
      conversation.addUserText(userText);
      while (true) {
        const stream = await gemini.interactions.create({
          model,
          input: conversation.steps,
          system_instruction: SYSTEM_INSTRUCTION,
          tools,
          stream: true,
        });
        const reply = await conversation.readReply(stream);
        if (reply.functionCalls.length === 0) return reply.text;
        conversation.addFunctionResults(
          await Promise.all(
            reply.functionCalls.map((call) => runFunctionCall(runner, call, toolsByName)),
          ),
        );
      }
    }

    return {
      ask: async ({ text }) => ({ text: await handleChatTurn(text) }),
      set_model: async (message) => {
        model = message.model;
        return { text: `Model set to **${message.model}**.` };
      },
    };
  },
});
