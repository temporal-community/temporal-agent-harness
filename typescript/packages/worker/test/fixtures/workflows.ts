// Probe agents for the workflow-level tests. One bundle for every test file; each file runs it on
// its own task queue.

import {
  CancellationScope,
  condition,
  defineQuery,
  defineSignal,
  isCancellation,
  proxyActivities,
  setHandler,
  sleep,
  workflowInfo,
} from "@temporalio/workflow";
import { z } from "zod";
import {
  AutoApprovalCriteria,
  CallbackToolError,
  FileTree,
  InMemoryFileSystem,
  MidTurn,
  Mount,
  TextMessage,
  TextReply,
  ToolApprovalDenied,
  ToolApprovalPolicy,
  activityTool,
  tool,
  codeModeTool,
  defineAgent,
  handler,
  injected,
  jevEvaluator,
  state,
  subagentToolset,
  type AgentWorkflowRunner,
  type AutoApprovalContext,
  type AutoApprovalDecision,
  type AutoModeEvaluator,
} from "@temporalio/agent-harness";

const TextInput = z.object({ text: z.string() });
const lastReplyQuery = defineQuery<string | null>("last_reply");
const seenQuery = defineQuery<string[]>("seen");
const releaseSignal = defineSignal<[string]>("release");

function denied(e: unknown): string {
  if (e instanceof ToolApprovalDenied || e instanceof CallbackToolError)
    return `${e instanceof CallbackToolError ? "error" : "denied"}:${e.reason}`;
  throw e;
}

// -- approvals ----------------------------------------------------------------------------------

export const gatedActivityTool = activityTool({
  name: "gated_activity_tool",
  description: "A non-safe activity tool: gated unless the policy allows it.",
  inputSchema: TextInput,
  outputSchema: z.string(),
});
export const safeActivityTool = activityTool({
  name: "safe_activity_tool",
  description: "An inherently safe activity tool.",
  inputSchema: TextInput,
  outputSchema: z.string(),
  inherentlySafe: true,
});
const gatedWorkflowTool = tool({
  name: "gated_workflow_tool",
  description: "A non-safe inline tool.",
  inputSchema: TextInput,
  outputSchema: z.string(),
  execute: async ({ text }) => `wf:${text}`,
});

const act = handler({
  description: "Run one probe scenario.",
  input: TextMessage,
  output: TextReply,
});

function approvalProbe(runner: AgentWorkflowRunner) {
  let lastReply: string | null = null;
  setHandler(lastReplyQuery, () => lastReply);
  const call = (id: string, tool: typeof gatedActivityTool, text: string) =>
    runner.runTool(id, tool, { text }).catch(denied);
  return {
    async act({ text }: { text: string }) {
      if (text === "concurrent") {
        const results = await Promise.all([
          runner.runTool("act-A", gatedActivityTool, { text: "A" }),
          runner.runTool("act-B", gatedActivityTool, { text: "B" }),
        ]);
        lastReply = results.join("|");
      } else if (text === "pair") {
        lastReply = (
          await Promise.all([
            call("g1", gatedActivityTool, "S"),
            runner.runTool("wf-1", gatedWorkflowTool, { text: "Z" }).catch(denied),
          ])
        ).join("|");
      } else if (text === "safe") lastReply = await call("s1", safeActivityTool, "S");
      else if (text.startsWith("arg:"))
        lastReply = await call("g1", gatedActivityTool, text.slice("arg:".length));
      else if (text === "workflow-tool")
        lastReply = await runner.runTool("wf-1", gatedWorkflowTool, { text: "Z" }).catch(denied);
      else lastReply = await call("g1", gatedActivityTool, "S");
      return { text: lastReply };
    },
  };
}

export const EVALUATED_CRITERIA = AutoApprovalCriteria.parse({
  sets: {
    evaluated: {
      effect: "Whatever the probe's tool does.",
      escalate_when: ["the evaluator under test says so"],
    },
  },
  default: "evaluated",
});

async function approveGatedActivityTool(ctx: AutoApprovalContext): Promise<AutoApprovalDecision> {
  return {
    verdict: ctx.tool_name === "gated_activity_tool" ? "approved" : "user-approval",
    reason: "auto-approved by the evaluator",
  };
}

async function verdictEvaluator(ctx: AutoApprovalContext): Promise<AutoApprovalDecision> {
  if (ctx.tool_description !== "A non-safe activity tool: gated unless the policy allows it.")
    throw new Error("wrong tool description");
  if (ctx.tool_input.text === "deny") return { verdict: "denied", reason: "policy forbids 'deny'" };
  if (ctx.tool_input.text === "escalate") return { verdict: "not-applicable", reason: "not sure" };
  return { verdict: "approved", reason: "looks fine" };
}

const evaluatorCancelledQuery = defineQuery<boolean>("evaluator_cancelled");
async function denyOnceReleased(): Promise<AutoApprovalDecision> {
  let cancelled = false;
  setHandler(evaluatorCancelledQuery, () => cancelled);
  try {
    await condition(() => false);
  } catch (e) {
    if (isCancellation(e)) cancelled = true;
    throw e;
  }
  return { verdict: "denied", reason: "approver says no" };
}

async function explodingEvaluator(): Promise<AutoApprovalDecision> {
  throw new Error("approver is broken");
}

const nonsenseEvaluator = (async () => "yes please") as unknown as AutoModeEvaluator;

export const ApprovalProbeAgent = defineAgent({
  name: "ApprovalProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.alwaysRequireHumanApproval(),
  handlers: { act },
  create: ({ runner }) => approvalProbe(runner),
});

const evaluatorProbe = (name: string, autoModeEvaluator: AutoModeEvaluator) =>
  defineAgent({
    name,
    approvalPolicyDefault: ToolApprovalPolicy.autoMode(),
    autoApprovalCriteriaDefault: EVALUATED_CRITERIA,
    autoModeEvaluator,
    handlers: { act },
    create: ({ runner }) => approvalProbe(runner),
  });

export const EvaluatorProbeAgent = evaluatorProbe("EvaluatorProbeAgent", approveGatedActivityTool);
export const VerdictEvaluatorProbeAgent = evaluatorProbe(
  "VerdictEvaluatorProbeAgent",
  verdictEvaluator,
);
export const SlowEvaluatorProbeAgent = evaluatorProbe("SlowEvaluatorProbeAgent", denyOnceReleased);
export const BrokenEvaluatorProbeAgent = evaluatorProbe(
  "BrokenEvaluatorProbeAgent",
  explodingEvaluator,
);
export const NonsenseEvaluatorProbeAgent = evaluatorProbe(
  "NonsenseEvaluatorProbeAgent",
  nonsenseEvaluator,
);

let evaluated: string[] = [];
async function recordingEvaluator(ctx: AutoApprovalContext): Promise<AutoApprovalDecision> {
  evaluated.push(`${ctx.tool_name}@${ctx.criteria_version}`);
  return { verdict: "approved", reason: "evaluated" };
}

/** An approval probe whose `policy` message changes the live policy or criteria, joining a running turn. */
export const PolicyProbeAgent = defineAgent({
  name: "PolicyProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.alwaysRequireHumanApproval(),
  autoModeEvaluator: recordingEvaluator,
  handlers: {
    act,
    policy: handler({
      description: "Change the live policy.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ACCEPT,
      modelCallable: false,
    }),
  },
  create: ({ runner }) => {
    evaluated = [];
    setHandler(seenQuery, () => evaluated);
    return {
      ...approvalProbe(runner),
      async policy({ text }) {
        const [verb, arg = ""] = text.split(":");
        if (verb === "allow")
          runner.setApprovalPolicy(ToolApprovalPolicy.withToolAllowed(runner.approvalPolicy, arg));
        else if (verb === "auto")
          runner.setApprovalPolicy(
            ToolApprovalPolicy.withAutoMode(runner.approvalPolicy, arg === "on"),
          );
        else if (verb === "assign") runner.assignToolCriteria("gated_activity_tool", arg);
        else if (verb === "criteria") runner.setAutoApprovalCriteria(EVALUATED_CRITERIA);
        return { text };
      },
    };
  },
});

// -- the builtin Jev evaluator ------------------------------------------------------------------

const AmountInput = z.object({ amount: z.number().int() });
export const refundCustomer = activityTool({
  name: "refund_customer",
  description: "Refund a customer. Moves real money.",
  inputSchema: AmountInput,
  outputSchema: z.string(),
  autoApprovalCriteria: "financial",
});
export const refundWithCredential = activityTool({
  name: "refund_with_credential",
  description: "Refund a customer using an injected credential.",
  inputSchema: AmountInput,
  outputSchema: z.string(),
  autoApprovalCriteria: "financial",
  injected: { api_token: injected<string>() },
});
const refundInlineWithCredential = tool({
  name: "refund_inline_with_credential",
  description: "Refund a customer inline using an injected credential.",
  inputSchema: AmountInput,
  outputSchema: z.string(),
  autoApprovalCriteria: "financial",
  injected: { api_token: injected<string>() },
  execute: async ({ amount }) => `refunded:${amount}`,
});

export const INJECTED_SECRET = "s3cr3t-injected-credential-must-never-reach-a-model";

export const JevApprovalProbeAgent = defineAgent({
  name: "JevApprovalProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.autoMode(),
  autoApprovalCriteriaDefault: AutoApprovalCriteria.parse({
    sets: {
      financial: {
        effect: "Moves real money out to a customer. Cannot be taken back.",
        approve_when: ["the amount is under 50"],
        deny_when: ["the amount is 50 or more"],
      },
    },
    context: { environment: "test" },
  }),
  autoModeEvaluator: jevEvaluator(),
  handlers: { act, act_with_credential: act, act_inline_with_credential: act },
  create: ({ runner }) => {
    const injections = { api_token: INJECTED_SECRET };
    const refund = async (run: () => Promise<string>) => ({ text: await run().catch(denied) });
    return {
      act: ({ text }) =>
        refund(() => runner.runTool("r1", refundCustomer, { amount: Number(text) })),
      act_with_credential: ({ text }) =>
        refund(() =>
          runner.runTool("r1", refundWithCredential, { amount: Number(text) }, { injections }),
        ),
      act_inline_with_credential: ({ text }) =>
        refund(() =>
          runner.runTool(
            "r1",
            refundInlineWithCredential,
            { amount: Number(text) },
            { injections },
          ),
        ),
    };
  },
});

/** Jev with only its start-to-close timeout overridden; the rest is the evaluator's defaults. */
export const JevTimeoutProbeAgent = defineAgent({
  name: "JevTimeoutProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.autoMode(),
  autoApprovalCriteriaDefault: AutoApprovalCriteria.parse({
    sets: { financial: { approve_when: ["the amount is under 50"] } },
  }),
  autoModeEvaluator: jevEvaluator({ activityOptions: { startToCloseTimeout: "10 seconds" } }),
  handlers: { act },
  create: ({ runner }) => ({
    act: async ({ text }) => ({
      text: await runner.runTool("r1", refundCustomer, { amount: Number(text) }).catch(denied),
    }),
  }),
});

// -- callback tools -----------------------------------------------------------------------------

const echoTool = tool({
  name: "echo_tool",
  description: "Echo on the client.",
  inputSchema: TextInput,
  outputSchema: z.object({ value: z.string() }),
});
const noteTool = tool({
  name: "note_tool",
  description: "Take a note on the client.",
  inputSchema: TextInput,
  outputSchema: z.string(),
});
const slowTool = tool({
  name: "slow_tool",
  description: "A callback nobody answers.",
  inputSchema: TextInput,
  outputSchema: z.object({ value: z.string() }),
  timeout: "2 seconds",
});

export const CallbackProbeAgent = defineAgent({
  name: "CallbackProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: { act },
  create: ({ runner }) => {
    let lastReply: string | null = null;
    setHandler(lastReplyQuery, () => lastReply);
    const run = async (
      id: string,
      tool: { invoke(input: { text: string }): Promise<string | { value: string }> },
      text: string,
    ) => {
      const result = await runner.runTool(id, tool, { text });
      return `ok:${typeof result === "string" ? result : result.value}`;
    };
    return {
      async act({ text }) {
        const scenario = {
          echo: () => run("cb-echo", echoTool, "hi"),
          note: () => run("cb-note", noteTool, "buy milk"),
          timeout: () => run("cb-timeout", slowTool, "x"),
        };
        lastReply = await scenario[text as keyof typeof scenario]().catch(denied);
        return { text: lastReply };
      },
    };
  },
});

// -- the turn loop ------------------------------------------------------------------------------

export const Greeting = z.object({ name: z.string() }).meta({ title: "Greeting" });
const Greeted = z.object({ message: z.string() }).meta({ title: "Greeted" });

export const TypedProbeAgent = defineAgent({
  name: "TypedProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    greet: handler({
      description: "Greet a person by name.",
      input: Greeting,
      output: Greeted,
      midTurn: MidTurn.ENQUEUE,
    }),
    pick: handler({
      description: "Pick a model.",
      input: z.object({ model: z.string() }),
      output: z.object({ model: z.string() }),
      midTurn: MidTurn.ENQUEUE,
    }),
    boom: handler({
      description: "Always fails.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
  },
  create: () => {
    const seen: string[] = [];
    setHandler(seenQuery, () => seen);
    return {
      greet: async ({ name }) => (seen.push(`greet:${name}`), { message: `hi ${name}` }),
      pick: async ({ model }) => (seen.push(`pick:${model}`), { model }),
      boom: async ({ text }) => {
        throw new Error(`boom: ${text}`);
      },
    };
  },
});

export const NoEvaluatorProbeAgent = defineAgent({
  name: "NoEvaluatorProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowTools(["get_order"]),
  handlers: {
    enable_auto_mode: handler({
      description: "Turn auto mode on.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
      modelCallable: false,
    }),
    greet: handler({
      description: "Greet a person by name.",
      input: Greeting,
      output: Greeted,
      midTurn: MidTurn.ENQUEUE,
    }),
  },
  create: ({ runner }) => {
    const seen: string[] = [];
    setHandler(seenQuery, () => seen);
    return {
      async enable_auto_mode() {
        runner.setApprovalPolicy(ToolApprovalPolicy.withAutoMode(runner.approvalPolicy, true));
        return { text: "unreachable" };
      },
      greet: async ({ name }) => (seen.push(`greet:${name}`), { message: `hi ${name}` }),
    };
  },
});

export const MidTurnProbeAgent = defineAgent({
  name: "MidTurnProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    work: handler({
      description: "Work until released.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
    steer: handler({
      description: "Steer the open turn.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ACCEPT,
    }),
    outlive: handler({
      description: "Outlive a sibling.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ACCEPT,
    }),
    exclusive: handler({
      description: "Only when idle.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.REJECT,
    }),
  },
  create: ({ runner }) => {
    const released = new Set<string>();
    const joined: boolean[] = [];
    let publishedAfterSibling = false;
    setHandler(releaseSignal, (which) => void released.add(which));
    setHandler(defineQuery<boolean[]>("joined"), () => joined);
    setHandler(defineQuery<boolean>("published_after_sibling"), () => publishedAfterSibling);
    setHandler(defineQuery<boolean>("closed"), () => runner.closed);
    return {
      async work({ text }) {
        await condition(() => released.has("work"));
        return { text: `worked:${text}` };
      },
      async steer({ text }, ctx) {
        joined.push(ctx.joined_turn);
        return { text: `steer:${text}:joined=${ctx.joined_turn}:turn=${ctx.turn_number}` };
      },
      async outlive({ text }) {
        await condition(() => released.has("outlive"));
        runner.publish({ type: "message_handler_end", output: { note: "still in the turn" } });
        publishedAfterSibling = true;
        return { text: `outlived:${text}` };
      },
      exclusive: async ({ text }) => ({ text: `exclusive:${text}` }),
    };
  },
});

export const TripData = z.object({ traveler: z.string() }).meta({ title: "TripData" });

export const LifecycleProbeAgent = defineAgent({
  name: "LifecycleProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  initData: { schema: TripData, required: false },
  handlers: {
    whoami: handler({
      description: "Who is travelling.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
  },
  create: ({ data }) => {
    const events: string[] = [];
    setHandler(defineQuery<string[]>("events"), () => events);
    return {
      async setup() {
        await sleep("10 milliseconds");
        events.push("setup");
      },
      async teardown() {
        events.push("teardown");
      },
      async whoami() {
        const traveler = data?.traveler ?? "nobody";
        events.push(`whoami:${traveler}`);
        return { text: traveler };
      },
    };
  },
});

// -- observable state ---------------------------------------------------------------------------

const Step = z.object({ name: z.string(), done: z.boolean().default(false) });
export const PlanState = z.object({
  goal: z.string().default(""),
  steps: z.array(Step).default([]),
  scratch: z.record(z.string(), z.string()).default({}),
});

export const StateProbeAgent = defineAgent({
  name: "StateProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  states: { plan: state(PlanState) },
  handlers: {
    make_plan: handler({ description: "Make a plan.", input: TextMessage, output: TextReply }),
  },
  create: ({ state: { plan } }) => ({
    async make_plan({ text }) {
      plan.mutate((d) => {
        d.goal = text;
        d.steps.push({ name: "research", done: false }, { name: "write", done: false });
      });
      plan.mutate((d) => {
        d.steps[0]!.done = true;
        d.scratch.note = "research finished";
      });
      plan.mutate(() => {});
      plan.mutate((d) => {
        d.steps.splice(0, 1);
      });
      return { text: `${plan.current.steps.length} step(s) left` };
    },
  }),
});

// -- Code Mode ----------------------------------------------------------------------------------

const Item = z.object({ id: z.string(), price: z.number() }).meta({ title: "Item" });
export const searchItems = activityTool({
  name: "search",
  description: "Search the catalog for items.",
  inputSchema: z.object({ origin: z.string(), n: z.number().int() }),
  outputSchema: z.object({ items: z.array(Item) }).meta({ title: "SearchResp" }),
  inherentlySafe: true,
});
export const bookItem = activityTool({
  name: "book",
  description: "Book an item; returns a confirmation code.",
  inputSchema: z.object({ id: z.string() }),
  outputSchema: z.string(),
});

export const CodeModeProbeAgent = defineAgent({
  name: "CodeModeProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowInherentlySafe(),
  handlers: {
    run: handler({ description: "Run a Code Mode script.", input: TextMessage, output: TextReply }),
  },
  create: ({ runner }) => {
    const codeTool = codeModeTool([searchItems, bookItem, failingTool], { name: "run_code" });
    return {
      run: async ({ text }) => ({
        text: await runner.runTool("code-1", codeTool, { script: text }),
      }),
    };
  },
});

/** A Code Mode tool with a seeded read-only /skills and a writable /workspace tracked in state. Reads are allow-listed; writes ask. */
export const VfsProbeAgent = defineAgent({
  name: "VfsProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowTools(
    ["run_code", "fs_seed", "fs_stat", "fs_read", "fs_list"],
    { alsoInherentlySafe: true },
  ),
  states: { workspace: state(FileTree) },
  handlers: {
    run: handler({ description: "Run a Code Mode script.", input: TextMessage, output: TextReply }),
  },
  create: ({ runner, state: { workspace } }) => {
    const { load_skills } = proxyActivities<{ load_skills(): Promise<Record<string, string>> }>({
      startToCloseTimeout: "10 seconds",
    });
    const codeTool = codeModeTool([searchItems, bookItem], {
      name: "run_code",
      mounts: [
        new Mount("/skills", new InMemoryFileSystem({ seed: () => load_skills() }), {
          readOnly: true,
          description: "Skills, one folder each.",
        }),
        new Mount("/workspace", new InMemoryFileSystem({ maxBytes: 200, state: workspace }), {
          description: "Scratch space.",
        }),
      ],
    });
    return {
      run: async ({ text }) => ({
        text: await runner.runTool("code-1", codeTool, { script: text }),
      }),
    };
  },
});

// -- subagents ----------------------------------------------------------------------------------

export const EchoChildAgent = defineAgent({
  name: "EchoChildAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    echo: handler({
      description: "Echo the text back, upper-cased.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
      inputName: "request",
    }),
    fail: handler({
      description: "Always fails.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
  },
  create: () => ({
    echo: async ({ text }) => ({ text: text.toUpperCase() }),
    fail: async ({ text }) => {
      throw new Error(`child failed on ${text}`);
    },
  }),
});

export const SubagentParentAgent = defineAgent({
  name: "SubagentParentAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    delegate: handler({
      description: "Delegate to an echo subagent.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
  },
  create: ({ runner }) => {
    const tools = new Map(
      subagentToolset(EchoChildAgent, { key: "echo", taskQueue: workflowInfo().taskQueue }).map(
        (t) => [t.name, t],
      ),
    );
    const tool = (name: string) => tools.get(name)!;
    return {
      async delegate({ text }) {
        const handle = (await runner.runTool("start", tool("start_echo"), {})) as string;
        let reply: string;
        try {
          // echo takes its input as `request`, fail under the default `message`.
          const input =
            text === "fail"
              ? { subagent: handle, message: { text } }
              : { subagent: handle, request: { text } };
          const out = (await runner.runTool(
            "send",
            tool(text === "fail" ? "echo_fail" : "echo_echo"),
            input,
          )) as { text: string };
          reply = out.text;
        } catch (e) {
          reply = `error:${e instanceof Error && e.cause instanceof Error ? e.cause.message : String(e)}`;
        }
        await runner.runTool("stop", tool("stop_echo"), { subagent: handle });
        return { text: reply };
      },
    };
  },
});

/** An ApprovalProbeAgent whose own policy lets its gated tool run, so it runs as a subagent. */
export const AllowedToolProbeAgent = defineAgent({
  name: "AllowedToolProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowTools(["gated_activity_tool"]),
  handlers: { act },
  create: ({ runner }) => approvalProbe(runner),
});

/** Runs an AllowedToolProbeAgent child's gated tool and leaves the child running. */
export const ToolChildParentAgent = defineAgent({
  name: "ToolChildParentAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    delegate: handler({
      description: "Run the child's gated tool.",
      input: TextMessage,
      output: TextReply,
    }),
  },
  create: ({ runner }) => ({
    async delegate() {
      const [start, act] = subagentToolset(AllowedToolProbeAgent, {
        key: "probe",
        taskQueue: workflowInfo().taskQueue,
      });
      const handle = (await runner.runTool("start", start!, {})) as string;
      return (await runner.runTool("send", act!, { subagent: handle, message: { text: "x" } })) as {
        text: string;
      };
    },
  }),
});

/** Starts a BriefedAgent child whose initData callback returns null. */
export const NullBriefParentAgent = defineAgent({
  name: "NullBriefParentAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    delegate: handler({ description: "Start the child.", input: TextMessage, output: TextReply }),
  },
  create: ({ runner }) => ({
    async delegate() {
      const [start] = subagentToolset(BriefedAgent, {
        key: "brief",
        taskQueue: workflowInfo().taskQueue,
        initData: () => null,
      });
      return {
        text: await runner
          .runTool("start", start!, {})
          .then(String, (e: unknown) => `${(e as Error).name}:${(e as Error).message}`),
      };
    },
  }),
});

// -- the web API and the published client --------------------------------------------------------

const answerTool = tool({
  name: "answer_question",
  description: "Ask the user a question.",
  inputSchema: TextInput,
  outputSchema: z.object({ answer: z.string() }),
  inherentlySafe: true,
});
const Notes = z.object({ notes: z.array(z.string()).default([]) }).meta({ title: "Notes" });

export const ContractAgent = defineAgent({
  name: "ContractAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowInherentlySafe(),
  states: { notes: state(Notes) },
  handlers: {
    ask: handler({
      description: "Chat with the contract agent.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
    exclusive: handler({
      description: "Only when idle.",
      input: TextMessage,
      output: TextReply,
      midTurn: MidTurn.REJECT,
    }),
  },
  create: ({ runner, state: { notes } }) => ({
    async ask({ text }) {
      const parts: string[] = [];
      if (text.includes("approve"))
        parts.push(
          await runner.runTool("gated-1", gatedWorkflowTool, { text: "go" }).catch(denied),
        );
      if (text.includes("callback"))
        parts.push((await runner.runTool("cb-1", answerTool, { text: "Why?" })).answer);
      const reply = [`You said: ${text}`, ...parts].join(" | ");
      for (const word of reply.split(/(?<= )/)) runner.publish({ type: "reply_delta", text: word });
      notes.mutate((d) => {
        d.notes.push(text);
      });
      return { text: reply };
    },
    exclusive: async ({ text }) => ({ text: `exclusive:${text}` }),
  }),
});

export const BriefedAgent = defineAgent({
  name: "BriefedAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  initData: { schema: z.object({ topic: z.string() }).meta({ title: "Brief" }), required: true },
  handlers: {
    ask: handler({ description: "Ask about the topic.", input: TextMessage, output: TextReply }),
  },
  create: ({ data }) => ({ ask: async () => ({ text: data.topic }) }),
});

/** Replies with the principal it acts for, which only the server may set. */
export const ActingAgent = defineAgent({
  name: "ActingAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  initData: {
    schema: z.object({ principal: z.string().optional() }),
    required: false,
    serverOwned: ["principal"],
  },
  handlers: {
    ask: handler({ description: "Who do you act for?", input: TextMessage, output: TextReply }),
  },
  create: ({ data }) => ({ ask: async () => ({ text: data?.principal ?? "nobody" }) }),
});

// -- raw model streams ---------------------------------------------------------------------------

/** Streams raw OpenAI Agents events to `topic` the way a model activity does; see test/support.ts. */
export type RawModelProbe = (topic: string, scenario: "retry" | "abandon") => Promise<string>;
const { raw_model_probe } = proxyActivities<{ raw_model_probe: RawModelProbe }>({
  startToCloseTimeout: "30 seconds",
  retry: { initialInterval: "100 milliseconds", maximumAttempts: 2 },
});

export const RawStreamProbeAgent = defineAgent({
  name: "RawStreamProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    run: handler({
      description: "Make one raw-streamed model call.",
      input: TextMessage,
      output: TextReply,
    }),
  },
  create: ({ runner }) => ({
    run: async ({ text }) => ({
      text: await runner
        .turnHost([])
        .modelCall("openai-agents", "probe-model", (topic) =>
          raw_model_probe(topic, text as "retry" | "abandon"),
        )
        .catch(() => "model call failed"),
    }),
  }),
});

/** The TypeScript twin of test/interop/py_agent.py's PyEcho, for the wire parity test. */
export const InteropEcho = defineAgent({
  name: "InteropEcho",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    ask: handler({ description: "Echo the message back.", input: TextMessage, output: TextReply }),
  },
  create: ({ runner }) => ({
    ask: async ({ text }) => {
      runner.publish({ type: "reply_delta", text: `echo: ${text}` });
      return { text: `echo: ${text}` };
    },
  }),
});

/** Drives one turn of a PyEcho child (a Python agent) on `task_queue`; the child closes with it. */
export const InteropParent = defineAgent({
  name: "InteropParent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  handlers: {
    ask: handler({
      description: "Ask the Python child.",
      input: z.object({ text: z.string(), task_queue: z.string() }),
      output: TextReply,
    }),
  },
  create: ({ runner }) => ({
    ask: async ({ text, task_queue }) => {
      const child = await runner.startSubagent("py", "PyEcho", task_queue);
      // Not stopped: a closed child's stream can no longer be read by a reader still merging it.
      return { text: String((await runner.runSubagentTurn(child, "ask", { text })).text) };
    },
  }),
});

// -- payload size --------------------------------------------------------------------------------

export const bigResult = activityTool({
  name: "big_result",
  description: "Return n characters.",
  inputSchema: z.object({ n: z.number().int() }),
  outputSchema: z.string(),
  inherentlySafe: true,
});

/** `reply:N`, `tool:N`, `py:<script>`; anything else replies with its length. */
export const PayloadProbeAgent = defineAgent({
  name: "PayloadProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowInherentlySafe(),
  handlers: {
    run: handler({
      description: "Run one payload-size scenario.",
      input: TextMessage,
      output: TextReply,
    }),
  },
  create: ({ runner }) => {
    const py = codeModeTool([bigResult], { name: "run_py" });
    return {
      run: async ({ text }) => {
        const colon = text.indexOf(":");
        const [kind, arg] = colon < 0 ? ["", text] : [text.slice(0, colon), text.slice(colon + 1)];
        if (kind === "reply") return { text: "x".repeat(Number(arg)) };
        if (kind === "tool")
          return {
            text: `len:${(await runner.runTool("big-1", bigResult, { n: Number(arg) })).length}`,
          };
        if (kind === "py") return { text: await runner.runTool("py-1", py, { script: arg }) };
        return { text: `len:${text.length}` };
      },
    };
  },
});

// -- activity tool retries and heartbeats -------------------------------------------------------

export const failingTool = activityTool({
  name: "failing_tool",
  description: "Always throws.",
  inputSchema: TextInput,
  outputSchema: z.string(),
  inherentlySafe: true,
  activityOptions: {
    startToCloseTimeout: "30 seconds",
    retry: { initialInterval: "10 milliseconds", maximumAttempts: 3 },
  },
});
export const heartbeatTool = activityTool({
  name: "heartbeat_tool",
  description: "Runs for longer than its heartbeat timeout.",
  inputSchema: TextInput,
  outputSchema: z.string(),
  inherentlySafe: true,
  activityOptions: {
    startToCloseTimeout: "30 seconds",
    heartbeatTimeout: "1 second",
    retry: { maximumAttempts: 1 },
  },
});

/** heartbeat_tool's body (2.5 s) under a start-to-close timeout it cannot finish within. */
const timingOutTool = activityTool({
  name: "heartbeat_tool",
  description: "Runs for longer than its start-to-close timeout.",
  inputSchema: TextInput,
  outputSchema: z.string(),
  inherentlySafe: true,
  activityOptions: {
    startToCloseTimeout: "1 second",
    heartbeatTimeout: "500 milliseconds",
    retry: { maximumAttempts: 1 },
  },
});

export const ToolRetryAgent = defineAgent({
  name: "ToolRetryAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowInherentlySafe(),
  handlers: { act },
  create: ({ runner }) => ({
    async act({ text }) {
      const tool =
        text === "slow" ? heartbeatTool : text === "timeout" ? timingOutTool : failingTool;
      return {
        text: await runner
          .runTool("t1", tool, { text })
          .catch((e: unknown) => `caught:${e instanceof Error ? e.name : String(e)}`),
      };
    },
  }),
});

// -- cancellation inside a handler ---------------------------------------------------------------

const parkedInline = tool({
  name: "parked_inline",
  description: "Waits until cancelled.",
  inputSchema: z.object({}),
  outputSchema: z.string(),
  inherentlySafe: true,
  execute: async () => {
    await condition(() => false);
    return "never";
  },
});

const chainedFailure = tool({
  name: "chained_failure",
  description: "Throws an error with a cause.",
  inputSchema: z.object({}),
  outputSchema: z.string(),
  inherentlySafe: true,
  execute: async () => {
    throw new Error("outer", { cause: new Error("inner") });
  },
});

/** Handlers that cancel their own work: a timeout of their own, a tool call their scope cancels. */
export const CancellationProbeAgent = defineAgent({
  name: "CancellationProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowInherentlySafe(),
  handlers: { act },
  create: ({ runner }) => ({
    async act({ text }) {
      const withinTimeout = <T>(fn: () => Promise<T>) => CancellationScope.withTimeout(50, fn);
      if (text === "own-timeout") await withinTimeout(() => condition(() => false));
      if (text === "activity")
        await withinTimeout(() => runner.runTool("t1", heartbeatTool, { text }));
      if (text === "chained") await runner.runTool("t1", chainedFailure, {});
      if (text === "inline") {
        const caught = await withinTimeout(() => runner.runTool("t1", parkedInline, {})).catch(
          (e: unknown) => `caught:${(e as Error).name}`,
        );
        return { text: caught };
      }
      return { text: "done" };
    },
  }),
});
