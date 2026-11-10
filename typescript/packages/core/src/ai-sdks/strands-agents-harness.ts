// Harness glue for the Strands Agents SDK, workflow side (Python's strands_agents_harness.py, the
// half that runs in the workflow): `harnessStreamRoute` routing each model call to the in-flight
// turn, `asStrandsTool(s)` adapting harness tools onto Strands, the governance that runs each
// `TemporalMCPClient` tool call as a harness tool, and `harnessStrandsAgent`, which builds a
// `TemporalAgent` wired to all of it that refuses Strands interrupts. The worker half (the
// observer that turns each model call's stream into turn events) is
// `@temporalio/agent-harness-worker/ai-sdks/strands-agents-harness`.
//
// Only harness tools (`tool`, `activityTool`, `callbackTool`) and `TemporalMCPClient` handles are
// accepted. A person's input comes from a callback tool, whose call waits in the workflow until a
// client answers, and a person's approval of a tool call from the agent's approval policy, so
// Strands interrupts, which stop the run and re-run the tool on resume, are refused.

import {
  BeforeToolCallEvent,
  TextBlock,
  Tool,
  ToolResultBlock,
  type AgentResult,
  type AgentStreamEvent,
  type InvokeArgs,
  type InvokeOptions,
  type JSONSchema,
  type ToolContext,
  type ToolSpec,
  type ToolStreamGenerator,
} from "@strands-agents/sdk";
import { ApplicationFailure, isCancellation } from "@temporalio/workflow";
import { z } from "zod";
import { tool as harnessTool } from "../harness/agent.ts";
import { validate, type TurnStreamContext } from "../harness/agent-protocol/index.ts";
import { errorMessage } from "../harness/agent-workflow/errors.ts";
import type { AgentWorkflowRunner } from "../harness/agent-workflow.ts";
import type { AnyHarnessTool } from "../harness/agent-workflow/types.ts";
import {
  TemporalAgent,
  TemporalMCPClient,
  TemporalMCPTool,
  type TemporalAgentOptions,
} from "./strands-agents/index.ts";

/** The failure type of a harness Strands agent's run that a Strands interrupt ended. */
export const STRANDS_INTERRUPT_NOT_SUPPORTED = "StrandsInterruptNotSupported";

const INTERRUPT_REFUSAL =
  "Strands interrupts are not supported in a harness Strands agent. To get input from a person " +
  "or a client, declare a callbackTool and pass it through asStrandsTool(runner, ...); its call " +
  "waits in the workflow for the result. To gate tool calls on a person's approval, use the " +
  "agent's tool approval policy.";

// ---------------------------------------------------------------------------------------------
// The streaming route: which turn a model call's events go to
// ---------------------------------------------------------------------------------------------

/**
 * The routing token the harness hands the streaming model activity (Python's
 * `_HarnessStreamToken`): the in-flight turn's stream context and the model id, so the observer
 * can open the model-interaction bracket, naming the model, at dispatch.
 */
export interface HarnessStreamToken {
  context: TurnStreamContext;
  model: string | null;
}

/**
 * The `streamRoute` that sends an agent's model calls to `runner`'s current turn. `TemporalModel`
 * calls it in workflow code before each model call; outside a turn it returns `undefined`, and
 * the call runs without live streaming.
 */
export function harnessStreamRoute(
  runner: AgentWorkflowRunner,
): (model: string | undefined) => HarnessStreamToken | undefined {
  return (model) => {
    const context = runner.currentStreamContext;
    return context === null ? undefined : { context, model: model ?? null };
  };
}

// ---------------------------------------------------------------------------------------------
// Tool adapter: harness tools -> Strands Tools
// ---------------------------------------------------------------------------------------------

const HARNESS_TOOL_KINDS = new Set(["inline", "activity", "callback"]);

export interface StrandsToolOptions {
  /** Workflow-supplied values for the tools' injected parameters. */
  injections?: Record<string, unknown>;
}

/** A harness tool presented to Strands; every call runs through `runner.runTool`. */
class HarnessStrandsTool extends Tool {
  override readonly name: string;
  override readonly description: string;
  override readonly toolSpec: ToolSpec;
  readonly #runner: AgentWorkflowRunner;
  readonly #tool: AnyHarnessTool;
  readonly #injections: Record<string, unknown> | undefined;

  constructor(
    runner: AgentWorkflowRunner,
    tool: AnyHarnessTool,
    injections: Record<string, unknown> | undefined,
  ) {
    super();
    requireHarnessTool(tool);
    this.#runner = runner;
    this.#tool = tool;
    this.#injections = injections;
    this.name = tool.name;
    this.description = tool.description;
    // The tool's model-facing schema leaves its injected parameters out.
    this.toolSpec = {
      name: tool.name,
      description: tool.description,
      inputSchema: tool.parameters as JSONSchema,
    };
  }

  // eslint-disable-next-line require-yield
  override async *stream(toolContext: ToolContext): ToolStreamGenerator {
    const { toolUseId, input } = toolContext.toolUse;
    const parsed = validate(this.#tool.inputSchema, input);
    if (!parsed.ok)
      return this.#result(
        toolUseId,
        "error",
        `Payload for tool '${this.name}' does not match its schema: ${parsed.message}`,
      );
    try {
      const result = await this.#runner.runTool(
        toolUseId,
        this.#tool,
        input,
        this.#injections ? { injections: this.#injections } : {},
      );
      return this.#result(toolUseId, "success", stringify(result));
    } catch (e) {
      if (isCancellation(e)) throw e;
      return this.#result(toolUseId, "error", `Tool '${this.name}' failed: ${errorMessage(e)}`);
    }
  }

  /** The tool result, with any queued steering appended, claimed for this tool call. */
  #result(toolUseId: string, status: "success" | "error", text: string): ToolResultBlock {
    const steering = this.#runner.claimSteering({ toolId: toolUseId });
    return new ToolResultBlock({
      toolUseId,
      status,
      content: [new TextBlock(steering?.appendTo(text) ?? text)],
    });
  }
}

/**
 * Adapt a harness tool (`tool`, `activityTool`, `callbackTool`, or a `subagentToolset` entry) into
 * a Strands `Tool`. Each call runs `runner.runTool(...)` under the call's `toolUseId`, so the
 * harness applies the approval policy, publishes `tool_start` / `tool_end` / `tool_error`, fills
 * injected parameters from `injections`, and appends any steering to the result. A failed call
 * goes back to the model as an error result.
 */
export function asStrandsTool(
  runner: AgentWorkflowRunner,
  tool: AnyHarnessTool,
  { injections }: StrandsToolOptions = {},
): Tool {
  return new HarnessStrandsTool(runner, tool, injections);
}

/** Adapt several harness tools; see `asStrandsTool`. */
export function asStrandsTools(
  runner: AgentWorkflowRunner,
  tools: readonly AnyHarnessTool[] | Readonly<Record<string, AnyHarnessTool>>,
  options?: StrandsToolOptions,
): Tool[] {
  const list: readonly AnyHarnessTool[] = Array.isArray(tools)
    ? tools
    : Object.values(tools as Readonly<Record<string, AnyHarnessTool>>);
  return list.map((t) => asStrandsTool(runner, t, options));
}

function requireHarnessTool(tool: AnyHarnessTool): void {
  const candidate = tool as Partial<AnyHarnessTool> | null;
  if (
    candidate !== null &&
    typeof candidate === "object" &&
    HARNESS_TOOL_KINDS.has(candidate.kind ?? "") &&
    typeof candidate.invoke === "function"
  )
    return;
  throw new TypeError(
    `${String(candidate?.name ?? candidate)} is not a harness tool; declare it with tool(...), ` +
      "activityTool(...), callbackTool(...), or use subagentToolset(...).",
  );
}

function stringify(result: unknown): string {
  return typeof result === "string" ? result : JSON.stringify(result ?? null);
}

// ---------------------------------------------------------------------------------------------
// Governance: MCP tools run as harness tools
// ---------------------------------------------------------------------------------------------

/** Any object: the model's input reaches the MCP tool as it was sent. */
const ANY_INPUT = z.record(z.string(), z.unknown());

/**
 * An MCP tool whose every call runs as a harness `tool(...)` named and described as it is, through
 * `runner.runTool` under the call's `toolUseId`. The harness tool applies the approval policy and
 * publishes `tool_start` and `tool_end` or `tool_error`, exactly as for a declared tool. The model
 * gets the MCP tool's own result with any steering appended; a denied or failed call reaches it as
 * an error result.
 */
class GovernedStrandsTool extends Tool {
  override readonly name: string;
  override readonly description: string;
  override readonly toolSpec: ToolSpec;
  readonly #runner: AgentWorkflowRunner;
  readonly #tool: Tool;

  constructor(runner: AgentWorkflowRunner, inner: Tool) {
    super();
    this.#runner = runner;
    this.#tool = inner;
    this.name = inner.name;
    this.description = inner.description;
    this.toolSpec = inner.toolSpec;
  }

  // eslint-disable-next-line require-yield
  override async *stream(toolContext: ToolContext): ToolStreamGenerator {
    const { toolUseId, input } = toolContext.toolUse;
    let outcome: ToolResultBlock | undefined;
    const call = harnessTool({
      name: this.name,
      description: this.description,
      inputSchema: ANY_INPUT,
      execute: async () => {
        outcome = await resultOf(this.#tool.stream(toolContext));
        const text = resultText(outcome);
        if (outcome.status === "error") throw new Error(text);
        return text;
      },
    });
    let result: ToolResultBlock;
    try {
      await this.#runner.runTool(toolUseId, call, input);
      result = outcome!;
    } catch (e) {
      if (isCancellation(e)) throw e;
      result =
        outcome ??
        new ToolResultBlock({
          toolUseId,
          status: "error",
          content: [new TextBlock(`Tool '${this.name}' failed: ${errorMessage(e)}`)],
        });
    }
    const steering = this.#runner.claimSteering({ toolId: toolUseId });
    if (steering === undefined) return result;
    return new ToolResultBlock({
      toolUseId,
      status: result.status,
      content: [...result.content, new TextBlock(steering.render())],
    });
  }
}

/** Run a Strands tool's stream to its result. */
async function resultOf(stream: ToolStreamGenerator): Promise<ToolResultBlock> {
  for (;;) {
    const next = await stream.next();
    if (next.done) return next.value;
  }
}

/** A tool result's text blocks, or its content as JSON when it has none. */
function resultText(result: ToolResultBlock): string {
  const texts = result.content.flatMap((block) => (block instanceof TextBlock ? [block.text] : []));
  return texts.length > 0 ? texts.join("\n") : JSON.stringify(result.content);
}

// ---------------------------------------------------------------------------------------------
// The agent
// ---------------------------------------------------------------------------------------------

/**
 * A `TemporalAgent` that refuses Strands interrupts. A tool or a hook that interrupts ends the run
 * with `stopReason: 'interrupt'`, waiting to be resumed with answers; under the harness, callback
 * tools and the approval policy take that role, so such a run fails with a non-retryable
 * `ApplicationFailure`. `invoke` drains `stream`, so this covers both.
 */
class HarnessStrandsAgent extends TemporalAgent {
  override async *stream(
    args: InvokeArgs,
    options?: InvokeOptions,
  ): AsyncGenerator<AgentStreamEvent, AgentResult, undefined> {
    const result = yield* super.stream(args, options);
    if (result.stopReason === "interrupt")
      throw ApplicationFailure.create({
        type: STRANDS_INTERRUPT_NOT_SUPPORTED,
        nonRetryable: true,
        message: INTERRUPT_REFUSAL,
      });
    return result;
  }
}

export type HarnessStrandsAgentOptions = Omit<
  TemporalAgentOptions,
  "tools" | "streamingTopic" | "streamRoute"
> & {
  /**
   * Harness tools adapted with `asStrandsTool(s)`, and `TemporalMCPClient` handles to MCP servers
   * registered on the worker.
   */
  tools?: readonly (Tool | TemporalMCPClient)[];
};

/**
 * Build a `TemporalAgent` that streams onto `runner`'s turns and is fully governed.
 *
 * `tools` takes harness tools adapted with `asStrandsTool(s)` (from `tool`, `activityTool` or
 * `callbackTool`) and `TemporalMCPClient` handles to MCP servers registered on the worker with
 * `StrandsPlugin({ mcpClients })`; anything else is refused. Each MCP tool call runs as an
 * activity, under the agent's approval policy with `tool_start` / `tool_end` and steering, like a
 * harness tool. Every other option goes to `TemporalAgent`
 * (`model`, `activityOptions`, `systemPrompt`, `plugins`, ...), except `streamingTopic` and
 * `streamRoute`: a harness agent always streams onto its turn. Strands interrupts are refused:
 * get input from a person or client with a `callbackTool`.
 */
export function harnessStrandsAgent(
  runner: AgentWorkflowRunner,
  { tools = [], ...options }: HarnessStrandsAgentOptions = {},
): TemporalAgent {
  for (const key of ["streamingTopic", "streamRoute"])
    if (key in options)
      throw new TypeError(`harnessStrandsAgent sets the agent's streaming itself; remove ${key}`);
  for (const tool of tools)
    if (!(tool instanceof HarnessStrandsTool || tool instanceof TemporalMCPClient))
      throw new TypeError(
        `${toolLabel(tool)} is not a harness tool; a harness Strands agent takes only tool, ` +
          "activityTool and callbackTool tools adapted with asStrandsTool(runner, ...), and " +
          "TemporalMCPClient handles.",
      );
  const agent = new HarnessStrandsAgent({
    ...options,
    tools: [...tools],
    streamRoute: harnessStreamRoute(runner),
  });
  // MCP tools are listed before each model call, after this function returns, so each call is
  // governed as it starts. Added last, this hook sees the tool any other hook selected.
  agent.addHook(BeforeToolCallEvent, (event) => {
    // The tool Strands will run: one another hook selected, else the registry's match for the
    // call's (possibly rewritten) name.
    const selected =
      event.selectedTool ??
      (event.toolUse.name === event.tool?.name
        ? event.tool
        : event.agent.toolRegistry.get(event.toolUse.name));
    if (selected instanceof TemporalMCPTool)
      event.selectedTool = new GovernedStrandsTool(runner, selected);
  });
  const ungoverned = agent.tools.filter((t) => !(t instanceof HarnessStrandsTool));
  if (ungoverned.length > 0)
    throw new TypeError(
      `Tools ${ungoverned.map(toolLabel).join(", ")} were registered outside harness governance; ` +
        "a harness Strands agent takes only harness tools and TemporalMCPClient tools.",
    );
  return agent;
}

function toolLabel(tool: unknown): string {
  const named = tool as { name?: unknown } | null;
  return typeof named?.name === "string" ? named.name : String(tool);
}
