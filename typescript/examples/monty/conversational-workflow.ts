// Conversational travel agent that gives the model Code Mode over the travel tools (Python's
// conversational_workflow.py).
//
// The user chats in plain text; a *model in the loop* converses to gather what it needs, then
// **writes a Python script and runs it** to search and book flights/hotels, and replies in prose.
// It does this through the harness Code Mode feature: `codeModeTool` turns the travel activity
// tools into a single `run_travel_code` tool that executes a model-authored script in a sandbox,
// where each tool is an async host function the script calls. Every host call runs as a durable,
// approval-gated activity, and the script can combine many with real control flow (loops,
// `asyncio.gather` concurrency).
//
// The conversational front end is a Gemini Interactions tool-calling loop exposing that Code Mode
// tool and `ask_user`, a callback tool the model uses to ask the user something mid-turn. The
// workflow keeps the whole conversation and sends it on every call
// (InteractionConversation), so multi-turn conversation works. The Code Mode tool advertises the
// exact host-function signatures + result shapes in its own (generated) description, so the
// system prompt only needs to set the persona and point the model at the tool.

import { isCancellation } from "@temporalio/workflow";
import { z } from "zod";
import {
  MidTurn,
  TextMessage,
  TextReply,
  ToolApprovalPolicy,
  codeModeTool,
  defineAgent,
  handler,
  state,
  type AgentWorkflowRunner,
  type CodeModeTool,
} from "@temporalio/agent-harness";
import {
  InteractionConversation,
  functionParam,
  googleGenaiClient,
  type FunctionCallStep,
  type FunctionResultStepParam,
} from "@temporalio/agent-harness/ai-sdks/google-genai-plugin";
import { ALL_TOOLS } from "./activities.ts";
import { askUser } from "./ask-user.ts";
import { BOARD_TOOLS, BOARD_TOOL_NAMES, TripBoard } from "./trip-board.ts";

export const SUPPORTED_MODELS = ["gemini-3.8-flash", "gemini-3.1-flash-lite"] as const;
const DEFAULT_MODEL = SUPPORTED_MODELS[0];

// An enum (not a bare string) so the choice is enforced rather than merely suggested: the update
// boundary rejects anything else, and the same constraint shows up as an enum in this handler's
// JSON schema, which is what lets a generic client render a dropdown without knowing anything
// about Monty.
export const SetModel = z
  .object({ model: z.enum(SUPPORTED_MODELS).meta({ title: "Model" }) })
  .meta({
    title: "SetModel",
    description: "Which model this Monty session should use for subsequent turns.",
  });

const PolicyUpdate = z
  .object({
    new_policy: z
      .enum(["allow_safe", "strict", "dangerously_skip_all"])
      .meta({ title: "New Policy" }),
  })
  .meta({ title: "PolicyUpdate", description: "Selected tool approval policy name." });

export const SET_MODEL_DESCRIPTION = `Set the model this session uses for subsequent turns. Takes effect on the next
model call, so a turn already in flight finishes on the model it started with.`;

const SYSTEM_INSTRUCTION = `\
You are a friendly travel-booking assistant. You help users search and book flights and \
hotels and assemble trip itineraries. You don't have these abilities directly — instead you \
write small async Python scripts and run them with the \`run_travel_code\` tool, which exposes the \
travel operations as async host functions your script calls. The tool's description gives the \
exact host-function signatures and result shapes — follow them; index results with normal Python.

How to behave:
- Converse naturally. When you're missing something essential (origin/destination, dates, \
traveler name) or need the user to choose between options, ask them with the \`ask_user\` tool \
and carry on with their answers in the same turn. Ask ONE question per \`ask_user\` call; to ask \
several things, make one call per question in the same reply. Don't interrogate; make \
reasonable assumptions and state them.
- When you have enough to make progress, WRITE A SCRIPT and call \`run_travel_code\`. Run \
independent host calls concurrently with \`asyncio.gather\`; keep each script focused (search, or \
book, or summarize) so you can react to results.
- After a tool result, read it and reply to the user in plain, friendly prose — summarize \
options, prices, confirmations. You may run more scripts in follow-up turns as the \
conversation continues.
- Never invent flight/hotel ids or confirmation codes — only use ones returned by a script.

Keep the trip board current. The same script can call the board host functions — \
\`open_trip\`, \`record_booking\`, \`complete_trip_tasks\`, \`add_trip_tasks\`, \`set_trip_status\`, \
\`read_trip_board\` — and they are how the user watches you work. The board is not a summary you \
write at the end; it is the running state of the trip, so:
- \`open_trip\` as soon as you know which trip this is, BEFORE you search anything. Keep the \
\`trip_id\` it returns and use it for every later call (or pass "latest").
- In the SAME script that books something, \`record_booking\` it with the confirmation code the \
booking returned, and name the checklist items it finishes in \`completes\`. Never let the board \
show a booked flight next to an outstanding "book the flight".
- \`complete_trip_tasks\` as soon as a task is genuinely done, and \`add_trip_tasks\` the moment you \
find the trip needs something the plan did not have.
- \`set_trip_status\` to "booked" once nothing is left, or "cancelled" if the user calls it off.
- \`read_trip_board\` at the start of a follow-up request to recall where you left off.
Board calls are cheap and are not gated on the user's approval — there is nothing to approve \
about your own notes — so there is never a reason to batch them up or skip them.`;

/**
 * Run one model function call through `runTool` and return its result for the model: the result
 * as text (objects as JSON), or the error's message flagged `is_error`. `runTool` parks the call
 * id, so the tool's own lifecycle events correlate with the streaming activity's tool_requested.
 */
export async function runFunctionCall(
  runner: AgentWorkflowRunner,
  call: FunctionCallStep,
  toolsByName: ReadonlyMap<string, { invoke(input: never): Promise<unknown> }>,
): Promise<FunctionResultStepParam> {
  const base: FunctionResultStepParam = {
    type: "function_result",
    call_id: call.id,
    name: call.name,
    result: "",
  };
  if (call.signature) base.signature = call.signature;
  try {
    const tool = toolsByName.get(call.name);
    if (tool === undefined) throw new Error(`unknown tool: '${call.name}'`);
    const result = await runner.runTool(call.id, tool, call.arguments);
    return { ...base, result: typeof result === "string" ? result : JSON.stringify(result) };
  } catch (e) {
    if (isCancellation(e)) throw e;
    return { ...base, result: e instanceof Error ? e.message : String(e), is_error: true };
  }
}

export const MontyChatAgentTs = defineAgent({
  name: "MontyChatAgentTs",
  // Demo stance: require human approval for EVERY tool call that reaches the outside world, both
  // the `run_travel_code` tool and each travel host call the script makes (search/book flights &
  // hotels), since every call is dispatched through runTool and gated. The trip-board tools are
  // allowed by name, and only those: they write to the agent's own notes, so there is nothing for
  // a human to approve, and gating them would mean a click between every step and the board that
  // is supposed to be showing the steps. Named rather than `allowInherentlySafe()`, which would
  // also stop gating the run-code tool itself and take the script review off the table.
  // `ask_user` is allowed too: asking the user a question needs no approval.
  approvalPolicyDefault: ToolApprovalPolicy.allowTools([...BOARD_TOOL_NAMES, askUser.name]),
  states: { trip_board: state(TripBoard) },
  handlers: {
    ask: handler({
      description: `Chat with the travel assistant. Describe the trip you want (flights, hotels,
dates, traveler name) in plain text; the assistant converses, writes and runs Python
scripts against a simulated travel backend as needed, and replies with the results.`,
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
    // ACCEPT: reconfiguring the session is not work, so it should not wait behind work. It joins
    // the open turn and applies immediately, which is the whole point of being able to switch
    // models while the agent is mid-conversation. modelCallable false keeps it off a parent
    // agent's generated toolset by default: choosing the model is an operator's call, not
    // something a driving model should do to its own child.
    set_model: handler({
      description: SET_MODEL_DESCRIPTION,
      input: SetModel,
      output: TextReply,
      midTurn: MidTurn.ACCEPT,
      modelCallable: false,
    }),
    set_approval_policy: handler({
      description: "Update the agent's tool approval policy.",
      input: PolicyUpdate,
      output: TextReply,
      midTurn: MidTurn.ACCEPT,
      modelCallable: false,
      inputName: "policy_update",
    }),
  },
  create: ({ runner, state: { trip_board } }) => {
    let model: string = DEFAULT_MODEL;
    // The whole conversation, sent as the input of every model call (see InteractionConversation
    // for why it is not chained by interaction id).
    const conversation = new InteractionConversation();
    // The single model-facing tool: Code Mode over the travel tools. The model writes a Python
    // script that calls the travel operations as async host functions; each host call runs as a
    // durable, approval-gated activity via runTool.
    const codeTool: CodeModeTool = codeModeTool(
      // The travel tools plus the board tools, in one sandbox: a script can book a flight and
      // record it on the board without a round trip through the model, so the board cannot drift
      // from what was actually booked.
      [...ALL_TOOLS, ...BOARD_TOOLS],
      {
        name: "run_travel_code",
        // Hidden from the model and from the generated stubs: the script names the trip, never
        // the state it lives in.
        injections: { board: trip_board },
      },
    );
    // The model-facing tools: Code Mode, and asking the user mid-turn.
    const toolsByName = new Map<string, CodeModeTool | typeof askUser>([
      [codeTool.name, codeTool],
      [askUser.name, askUser],
    ]);
    // The Temporal-aware client from the Gemini plugin; the runner is wired in so reply text
    // streams to the workflow stream as it is generated.
    const gemini = googleGenaiClient({
      activityConfig: { startToCloseTimeout: "3 minutes" },
      runner,
    });

    /**
     * Run one conversational turn: stream the model, dispatch any tool calls, feed results back,
     * and loop until the model replies with no further calls.
     */
    async function handleChatTurn(userText: string): Promise<string> {
      const tools = [...toolsByName.values()].map(functionParam);
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
      set_approval_policy: async ({ new_policy }) => {
        const updated = {
          allow_safe: ToolApprovalPolicy.allowInherentlySafe,
          strict: ToolApprovalPolicy.alwaysRequireHumanApproval,
          dangerously_skip_all: ToolApprovalPolicy.dangerouslySkipAll,
        }[new_policy]();
        runner.setApprovalPolicy(updated);
        return { text: `Updated tool approval policy to: new_policy='${new_policy}'` };
      },
    };
  },
});
