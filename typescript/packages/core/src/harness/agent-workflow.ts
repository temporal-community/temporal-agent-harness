// AgentWorkflowRunner: the in-workflow engine every agent runs on. It owns the turn loop, message
// admission (MidTurn), the event stream, the tool-approval gate (policy -> auto mode -> human),
// callback tools, subagents, and observable state. `defineAgent` builds one per workflow run;
// authors reach it through the `runner` handed to their `create` function.
//
// Ambient context (Python's contextvars) is AsyncLocalStorage: the participant's message id is
// set once per handler task, and the in-flight tool call (runner, tool id, injections) is set by
// `runTool` for exactly one invocation. Both are read only inside this package.

import {
  ApplicationFailure,
  AsyncLocalStorage,
  CancellationScope,
  ParentClosePolicy,
  allHandlersFinished,
  condition,
  defaultPayloadConverter,
  defineQuery,
  defineSignal,
  defineUpdate,
  getExternalWorkflowHandle,
  isCancellation,
  log,
  proxyActivities,
  setHandler,
  startChild,
  uuid4,
  ActivityFailure,
} from "@temporalio/workflow";
import {
  WorkflowStream,
  decodePayloadWire,
  workflowStreamPublishSignal,
  type PublishInput,
  type WorkflowTopicHandle,
} from "@temporalio/workflow-streams/workflow";
import {
  AGENT_ID_LENGTH,
  AGENT_INTERFACE_QUERY,
  AGENT_STATUS_QUERY,
  APPROVAL_SHORT_ID_LENGTH,
  AgentMessageSchema,
  AutoApprovalCriteria,
  ApprovalVerdict,
  WIRE_VERDICT,
  CLOSE_SIGNAL,
  CallbackResultSchema,
  DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT,
  DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT,
  MessageDisposition,
  MidTurn,
  PROVIDE_CALLBACK_RESULT_UPDATE,
  SEND_AGENT_MESSAGE_UPDATE,
  TOOL_APPROVAL_UPDATE,
  TURN_EVENTS_TOPIC,
  ToolApprovalDecisionSchema,
  ToolApprovalPolicy,
  toJsonSchema,
  validate,
  type AcceptedFunction,
  type AgentConfig,
  type AgentEvent,
  type AgentMessageReply,
  type AgentStatus,
  type AgentStreamItem,
  type AutoApprovalContext,
  type AutoApprovalDecision,
  type AutoModeEvaluator,
  type CallbackResult,
  type CallbackResultAck,
  type MessageContext,
  type RunSubagentTurnInput,
  type Schema,
  type SubagentTurnResult,
  type ToolApprovalDecision,
  type ToolApprovalResult,
  type TurnStreamContext,
} from "./agent-protocol/index.ts";
import {
  MODEL_CALL_ABANDONED,
  RawEventTranslator,
  parseRawTopic,
  rawEventsTopic,
} from "../ai-sdks/integration-helpers/raw-events.ts";
import { StateRef, type StateEvent } from "./state/index.ts";
import {
  CallbackToolError,
  ToolApprovalDenied,
  assertAutoModeHasEvaluator,
  assertPortableJson,
  errorMessage,
  errorText,
  isOwnCancellation,
} from "./agent-workflow/errors.ts";
import type {
  Admission,
  AnyHarnessTool,
  AnyStateDecl,
  ApprovalEntry,
  CallbackEntry,
  HandlerDecl,
  HandlerImpl,
  RunnerOptions,
  SubagentInstance,
  TurnHost,
} from "./agent-workflow/types.ts";

export {
  CallbackToolError,
  ToolApprovalDenied,
  errorText,
};
export type {
  Admission,
  HandlerDecl,
  HandlerImpl,
  RunnerOptions,
  SubagentInstance,
  TurnHost,
};

/**
 * The child's accepted turn number, which the run_subagent_turn activity threads through the
 * failure's details; its absence is a protocol violation, not something to guess around.
 */
function acceptedTurnFromError(e: ApplicationFailure): number {
  for (const detail of e.details ?? []) {
    if (detail !== null && typeof detail === "object" && "subagent_turn" in detail)
      return Number(detail.subagent_turn);
  }
  throw ApplicationFailure.create({
    type: "SubagentProtocolError",
    nonRetryable: true,
    message: `${e.type} error from run_subagent_turn carried no 'subagent_turn' detail`,
    cause: e,
  });
}

// The workflow-local context a tool or handler reads: the runner and tool call it runs under, and
// the message it serves.

export interface ToolCall {
  readonly runner: AgentWorkflowRunner;
  readonly toolId: string;
  readonly injections: Readonly<Record<string, unknown>>;
}

export const messageIdStore = new AsyncLocalStorage<string>();
export const toolCallStore = new AsyncLocalStorage<ToolCall>();

/** The in-flight tool call, or throw: tools must be invoked through `runner.runTool`. */
export function currentToolCall(what: string): ToolCall {
  const call = toolCallStore.getStore();
  if (call === undefined)
    throw new Error(`${what} must be invoked via runner.runTool(...) within an active turn`);
  return call;
}

export const AMBIENT = Symbol("ambient message id");

function shortHex(length: number): string {
  return uuid4().replaceAll("-", "").slice(0, length);
}

const sendAgentMessageUpdate = defineUpdate<AgentMessageReply, [unknown]>(
  SEND_AGENT_MESSAGE_UPDATE,
);
const toolApprovalUpdate = defineUpdate<ToolApprovalResult, [unknown]>(TOOL_APPROVAL_UPDATE);
const provideCallbackResultUpdate = defineUpdate<CallbackResultAck, [unknown]>(
  PROVIDE_CALLBACK_RESULT_UPDATE,
);
const agentStatusQuery = defineQuery<AgentStatus>(AGENT_STATUS_QUERY);
const agentInterfaceQuery = defineQuery<AcceptedFunction[]>(AGENT_INTERFACE_QUERY);
const closeSignal = defineSignal(CLOSE_SIGNAL);

export class AgentWorkflowRunner {
  readonly agentId: string;
  readonly stream: WorkflowStream;
  readonly #events: WorkflowTopicHandle<AgentEvent>;
  readonly #handlers: Readonly<Record<string, HandlerDecl>>;
  readonly #evaluator: AutoModeEvaluator | undefined;

  #closed = false;
  #currentTurn = 0;
  #currentTurnId: string | null = null;
  #participants = 0;
  #pendingTurns: Admission[] = [];
  #joining: Admission[] = [];
  #policy: ToolApprovalPolicy;
  #criteria: AutoApprovalCriteria;
  #criteriaVersion = 0;
  readonly #approvals = new Map<string, ApprovalEntry>();
  readonly #callbacks = new Map<string, CallbackEntry>();
  readonly #subagents = new Map<string, SubagentInstance>();

  constructor(config: AgentConfig, options: RunnerOptions) {
    this.#handlers = options.handlers;
    this.#evaluator = options.autoModeEvaluator;
    this.#policy = config.approval_policy ?? options.approvalPolicyDefault;
    this.#criteria =
      config.auto_approval_criteria ??
      options.autoApprovalCriteriaDefault ??
      AutoApprovalCriteria.empty();
    this.agentId = config.agent_id ?? shortHex(AGENT_ID_LENGTH);
    // The constructor's own publish handler would take any publish buffered in the first
    // activation untranslated, so those are held here and translated below.
    const early: PublishInput[] = [];
    setHandler(workflowStreamPublishSignal, (input) => void early.push(input));
    this.stream = new WorkflowStream();
    this.#events = this.stream.topic<AgentEvent>(TURN_EVENTS_TOPIC);
    setHandler(workflowStreamPublishSignal, (input) => this.#onPublish(input));
    for (const input of early) this.#onPublish(input);
    // Before any handler is installed: a buffered first message is dispatched by setHandler, and
    // its message_accepted must not precede the state snapshots.
    this.state = this.#registerStates(options.states ?? {}, options.data);
    assertAutoModeHasEvaluator(this.#policy, this.#evaluator);
    if (this.#evaluator !== undefined && !this.#policy.auto_mode_enabled) {
      log.warn(
        "An autoModeEvaluator is wired but the live ToolApprovalPolicy has auto_mode_enabled=False, so it " +
          "will never be consulted and every gated call goes to the human gate.",
      );
    }
    setHandler(sendAgentMessageUpdate, (m) => this.#handleSendAgentMessage(m), {
      validator: (m) => this.#validateSendAgentMessage(m),
    });
    setHandler(toolApprovalUpdate, (d) => this.#handleToolApproval(d), {
      validator: (d) => this.#validateToolApproval(d),
    });
    setHandler(provideCallbackResultUpdate, (r) => this.#handleProvideCallbackResult(r), {
      validator: (r) => this.#validateProvideCallbackResult(r),
    });
    setHandler(agentStatusQuery, () => this.status);
    setHandler(agentInterfaceQuery, () => this.#agentInterface());
    setHandler(closeSignal, () => {
      this.#closed = true;
    });
  }

  // -- public surface -------------------------------------------------------------------------

  get closed(): boolean {
    return this.#closed;
  }

  /** The live turn context, stamped with the ambient participant's message id; null when idle. */
  get currentStreamContext(): TurnStreamContext | null {
    if (this.#currentTurnId === null) return null;
    return {
      turn_id: this.#currentTurnId,
      turn_number: this.#currentTurn,
      message_id: messageIdStore.getStore() ?? null,
      agent_id: this.agentId,
    };
  }

  get approvalPolicy(): ToolApprovalPolicy {
    return this.#policy;
  }

  /** Install a new policy; pending calls it now allow-lists are approved in the same step. */
  setApprovalPolicy(policy: ToolApprovalPolicy): void {
    assertAutoModeHasEvaluator(policy, this.#evaluator);
    this.#policy = policy;
    for (const entry of this.#approvals.values()) {
      if (entry.status !== "pending") continue;
      if (
        ToolApprovalPolicy.autoApproves(policy, entry.tool_name, {
          inherentlySafe: entry.inherently_safe,
        })
      ) {
        this.#resolveAndPublish(entry.tool_id, true, "auto-approved by updated policy");
      }
    }
  }

  get autoApprovalCriteria(): AutoApprovalCriteria {
    return this.#criteria;
  }

  setAutoApprovalCriteria(criteria: AutoApprovalCriteria): void {
    this.#criteria = criteria;
    this.#criteriaVersion += 1;
  }

  assignToolCriteria(toolName: string, criteriaSet: string): void {
    this.setAutoApprovalCriteria(
      AutoApprovalCriteria.withToolAssigned(this.#criteria, toolName, criteriaSet),
    );
  }

  get status(): AgentStatus {
    return {
      agent_id: this.agentId,
      current_turn: this.#currentTurn,
      turn_active: this.#participants > 0,
      turn_participants: this.#participants,
      pending_turns: this.#pendingTurns.map((a, i) => ({
        turn_number: this.#currentTurn + i + 1,
        turn_id: a.turn_id,
        message_id: a.message_id,
        message: JSON.stringify({ type: a.message.type, payload: a.message.payload }),
      })),
      pending_approvals: [...this.#approvals.values()]
        .filter((e) => e.status === "pending")
        .map(({ tool_id, tool_name, tool_input, turn_number, short_id }) => ({
          tool_id,
          tool_name,
          tool_input,
          turn_number,
          short_id,
        })),
      pending_callbacks: [...this.#callbacks.values()]
        .filter((e) => e.status === "pending")
        .map(({ tool_id, tool_name, tool_input, output_schema, turn_number }) => ({
          tool_id,
          tool_name,
          tool_input,
          output_schema,
          turn_number,
        })),
      subagents: [...this.#subagents.values()].map(({ handle, agent_key, workflow_id }) => ({
        subagent_id: handle,
        agent_key,
        workflow_id,
      })),
      approval_policy: this.#policy,
      has_auto_approval_evaluator: this.#evaluator !== undefined,
    };
  }

  /** Publish a payload on the current turn, attributed to the ambient message. */
  publish(event: AgentStreamItem): void {
    const ctx = this.currentStreamContext;
    if (ctx === null) throw new Error("publish() called with no active turn");
    this.pub(ctx.turn_id, ctx.turn_number, event);
  }

  /**
   * Run one tool call with its id and injections in scope; every harness tool needs this. A tool
   * picked by a model at run time takes its (unvalidated) JSON arguments; the tool validates them.
   */
  runTool<I, O>(
    callId: string,
    tool: { name?: string; invoke(input: I): Promise<O> },
    input: I,
    options?: { injections?: Record<string, unknown> },
  ): Promise<O>;
  runTool<O>(
    callId: string,
    tool: { name?: string; invoke(input: never): Promise<O> },
    input: unknown,
    options?: { injections?: Record<string, unknown> },
  ): Promise<O>;
  runTool(
    callId: string,
    tool: { name?: string; invoke(input: unknown): Promise<unknown> },
    input: unknown,
    { injections = {} }: { injections?: Record<string, unknown> } = {},
  ): Promise<unknown> {
    return toolCallStore.run({ runner: this, toolId: callId, injections }, () =>
      tool.invoke(input),
    );
  }

  /** The surface an SDK integration drives one turn through: these tools, this runner. */
  turnHost(
    tools: readonly AnyHarnessTool[],
    { injections = {} }: { injections?: Record<string, unknown> } = {},
  ): TurnHost {
    const byName = new Map(tools.map((t) => [t.name, t]));
    return {
      tools,
      runTool: (name, input, options) => {
        const tool = byName.get(name);
        if (tool === undefined) return Promise.reject(new Error(`unknown tool: '${name}'`));
        return this.runTool(options?.toolId || `${name}-${uuid4()}`, tool, input, { injections });
      },
      emit: (event) => this.publish(event),
      modelCall: (format, model, call, options) => {
        const ctx = this.currentStreamContext;
        if (ctx === null)
          return Promise.reject(new Error("modelCall() called with no active turn"));
        const topic = rawEventsTopic(format, ctx, model, this.#modelCalls++);
        // A structured-output call's text is its output, not a reply, so it streams no reply deltas.
        if (options?.structuredOutput) this.#rawEvents.structuredOutput(topic);
        // No Event Group of its own: the turn's already tags the model activity, and an SDK's
        // tool loop runs inside `call`, so a model-call scope would mis-tag its tool commands.
        return call(topic).catch((err: unknown) => {
          // Its activity's stream may have stopped mid-call; this closes the call's bracket.
          this.#publishRaw(topic, MODEL_CALL_ABANDONED);
          throw err;
        });
      },
    };
  }

  #modelCalls = 0;
  readonly #rawEvents = new RawEventTranslator();

  /** Declared state: each snapshot was published at construction, then every committed mutation. */
  readonly state: Readonly<Record<string, StateRef<Record<string, unknown>>>>;

  #registerStates(
    decls: Readonly<Record<string, AnyStateDecl>>,
    data: unknown,
  ): Record<string, StateRef<Record<string, unknown>>> {
    const refs: Record<string, StateRef<Record<string, unknown>>> = {};
    for (const [stateId, decl] of Object.entries(decls)) {
      const ref = new StateRef(stateId, decl, (e) => this.#publishStateEvent(e), data);
      refs[stateId] = ref;
      this.#publishStateEvent(ref.snapshot());
    }
    return refs;
  }

  // -- the turn loop --------------------------------------------------------------------------

  async run(impls: Readonly<Record<string, HandlerImpl<HandlerDecl>>>): Promise<void> {
    try {
      while (!this.#closed) {
        await condition(
          () =>
            this.#canOpenTurn ||
            this.#joining.length > 0 ||
            this.#closed,
        );
        if (this.#closed) break;
        for (let joined = this.#joining.shift(); joined; joined = this.#joining.shift()) {
          void this.#runParticipant(impls, joined, true);
        }
        if (this.#canOpenTurn) {
          const admitted = this.#pendingTurns.shift()!;
          this.#currentTurn += 1;
          this.#currentTurnId = admitted.turn_id;
          this.#participants = 1;
          this.pub(admitted.turn_id, admitted.turn_number, { type: "turn_started" }, null);
          void this.#runParticipant(impls, admitted, false);
        }
      }
      await condition(() => this.#participants === 0);
    } catch (e) {
      if (isCancellation(e)) {
        await CancellationScope.nonCancellable(async () => {
          this.stream.detachPollers();
          await condition(allHandlersFinished);
        });
      }
      throw e;
    }
    // Parked polls are handed the last events (turn_end included) before the run completes.
    this.stream.detachPollers();
    await condition(allHandlersFinished);
  }

  // -- the stream -----------------------------------------------------------------------------

  // WorkflowStream keeps its log and dedup maps private; the runner shares them (its own publish
  // handler replaces the stream's). Revisit if the SDK exposes them.
  get #stream(): {
    log: unknown[];
    baseOffset: number;
    publisherSequences: Record<string, number>;
    publisherLastSeen: Record<string, number>;
    onPublish(input: PublishInput): void;
  } {
    return this.stream as never;
  }

  get #canOpenTurn(): boolean {
    return this.#pendingTurns.length > 0 && this.#participants === 0;
  }

  get #hasPendingWork(): boolean {
    return this.#participants > 0 || this.#pendingTurns.length > 0;
  }

  #runParticipant(
    impls: Readonly<Record<string, HandlerImpl<HandlerDecl>>>,
    admitted: Admission,
    joined: boolean,
  ) {
    return messageIdStore.run(admitted.message_id, async () => {
      const { turn_id, turn_number } = admitted;
      this.pub(turn_id, turn_number, { type: "message_handler_start" });
      try {
        const decl = this.#handlers[admitted.message.type]!;
        const input: unknown = decl.input.parse(admitted.message.payload);
        const ctx: MessageContext = {
          joined_turn: joined,
          turn_id,
          turn_number,
          message_id: admitted.message_id,
        };
        const result = await impls[admitted.message.type]!(input, ctx);
        const output = decl.output.safeParse(result);
        if (!output.success) {
          throw ApplicationFailure.create({
            type: "BadHandlerReturn",
            nonRetryable: true,
            message: `handler '${admitted.message.type}' returned a value that does not match its output schema: ${output.error.message}`,
          });
        }
        assertPortableJson(output.data, `Output of handler '${admitted.message.type}'`);
        this.pub(turn_id, turn_number, {
          type: "message_handler_end",
          output: output.data as Record<string, unknown>,
        });
      } catch (e) {
        // Only the workflow's own cancellation abandons the turn silently; a cancellation the
        // handler observed (its own timeout, a cancelled activity) is a failure like any other.
        if (isOwnCancellation(e)) {
          this.#leaveTurn();
          return;
        }
        this.pub(turn_id, turn_number, { type: "message_handler_error", message: errorMessage(e) });
      }
      if (this.#leaveTurn()) {
        this.pub(turn_id, turn_number, { type: "turn_end" }, null);
      }
    });
  }

  #leaveTurn(): boolean {
    this.#participants -= 1;
    if (this.#participants > 0) return false;
    this.#participants = 0;
    this.#currentTurnId = null;
    return true;
  }

  // -- send_agent_message ---------------------------------------------------------------------

  #validateSendAgentMessage(raw: unknown): void {
    const message = AgentMessageSchema.parse(raw);
    assertPortableJson(message.payload, `Payload for function '${message.type}'`);
    const handler = Object.hasOwn(this.#handlers, message.type)
      ? this.#handlers[message.type]
      : undefined;
    const known = Object.keys(this.#handlers).sort();
    if (handler === undefined) {
      throw ApplicationFailure.create({
        type: "UnknownFunction",
        nonRetryable: true,
        message: `Unknown function '${message.type}'. Known functions: [${known.map((k) => `'${k}'`).join(", ")}].`,
        details: [{ name: message.type, known }],
      });
    }
    const parsed = handler.input.safeParse(message.payload);
    if (!parsed.success) {
      throw ApplicationFailure.create({
        type: "MalformedMessage",
        nonRetryable: true,
        message: `Payload for function '${message.type}' does not match its input model. Validation error: ${parsed.error.message}`,
        details: [{ function: message.type, error: parsed.error.message }],
      });
    }
    if (handler.midTurn === MidTurn.REJECT && this.#hasPendingWork) {
      throw ApplicationFailure.create({
        type: "MidTurnRejected",
        nonRetryable: true,
        message: `Function '${message.type}' declares mid_turn='reject' and the agent is busy.`,
        details: [
          {
            function: message.type,
            current_turn: this.#currentTurn,
            turn_participants: this.#participants,
          },
        ],
      });
    }
  }

  #handleSendAgentMessage(raw: unknown): AgentMessageReply {
    const message = AgentMessageSchema.parse(raw);
    const acceptedOffset = this.#streamHead();
    const handler = this.#handlers[message.type]!;
    const messageId = uuid4();
    let turnId: string;
    let turnNumber: number;
    let disposition: MessageDisposition;
    if (
      handler.midTurn === MidTurn.ACCEPT &&
      this.#participants > 0 &&
      this.#currentTurnId !== null
    ) {
      this.#participants += 1;
      turnId = this.#currentTurnId;
      turnNumber = this.#currentTurn;
      disposition = MessageDisposition.JOINED;
      this.#joining.push({
        message,
        turn_id: turnId,
        turn_number: turnNumber,
        message_id: messageId,
      });
    } else {
      turnId = uuid4();
      turnNumber = this.#currentTurn + this.#pendingTurns.length + 1;
      disposition = this.#hasPendingWork ? MessageDisposition.QUEUED : MessageDisposition.OPENED;
      this.#pendingTurns.push({
        message,
        turn_id: turnId,
        turn_number: turnNumber,
        message_id: messageId,
        accepted_offset: acceptedOffset,
      });
    }
    this.pub(
      turnId,
      turnNumber,
      { type: "message_accepted", handler: message.type, payload: message.payload, disposition },
      messageId,
    );
    return {
      turn_number: turnNumber,
      turn_id: turnId,
      message_id: messageId,
      disposition,
      accepted_offset: acceptedOffset,
    };
  }

  // The stream's head offset at this instant (Python reads it through its private `_on_offset()`).
  #streamHead(): number {
    return this.#stream.baseOffset + this.#stream.log.length;
  }

  // -- the approval gate ----------------------------------------------------------------------

  /** @internal Enforce the approval policy for the in-flight tool call. Throws ToolApprovalDenied. */
  async applyApprovalPolicy(
    toolId: string,
    toolName: string,
    toolInput: Record<string, unknown>,
    opts: {
      inherentlySafe: boolean;
      description: string | null;
      autoApprovalCriteria: string | null;
    },
  ): Promise<void> {
    if (
      ToolApprovalPolicy.autoApproves(this.#policy, toolName, {
        inherentlySafe: opts.inherentlySafe,
      })
    )
      return;
    const ctx = this.currentStreamContext;
    if (ctx === null)
      throw new Error(
        `gated tool '${toolName}' has no active turn to publish its approval against`,
      );
    const used = new Set([...this.#approvals.values()].map((e) => e.short_id));
    let shortId = shortHex(APPROVAL_SHORT_ID_LENGTH);
    while (used.has(shortId)) shortId = shortHex(APPROVAL_SHORT_ID_LENGTH);
    this.#approvals.set(toolId, {
      tool_id: toolId,
      tool_name: toolName,
      tool_input: toolInput,
      turn_number: ctx.turn_number,
      turn_id: ctx.turn_id,
      message_id: ctx.message_id,
      short_id: shortId,
      inherently_safe: opts.inherentlySafe,
      status: "pending",
      reason: null,
      remember: false,
    });
    this.pub(ctx.turn_id, ctx.turn_number, {
      type: "tool_approval_requested",
      tool_id: toolId,
      tool_name: toolName,
      tool_input: toolInput,
    });

    const entry = this.#approvals.get(toolId)!;
    const autoCtx = this.#autoModeContext(toolName, toolInput, opts);
    const decision =
      autoCtx === null ? null : await this.#runAutoModeEvaluator(autoCtx, toolId, ctx);
    if (decision?.verdict === ApprovalVerdict.APPROVED)
      this.#resolveAndPublish(toolId, true, decision.reason ?? null);
    else if (decision?.verdict === ApprovalVerdict.DENIED)
      this.#resolveAndPublish(toolId, false, decision.reason ?? null);

    await condition(() => this.#isApprovalResolved(toolId) || this.#closed);
    // Resolutions publish at their resolution site, in causal order. Only a close that wakes a
    // still-pending gate is finalized (as an auto-deny) here.
    if (entry.status === "pending") {
      entry.status = "denied";
      entry.reason = this.#closed ? "agent closed before approval" : "approval unresolved";
      this.#publishApprovalResolved(entry);
    }
    if (entry.status !== "approved") throw new ToolApprovalDenied(toolName, entry.reason);
  }

  #isApprovalResolved(toolId: string): boolean {
    const entry = this.#approvals.get(toolId);
    return entry !== undefined && entry.status !== "pending";
  }

  #autoModeContext(
    toolName: string,
    toolInput: Record<string, unknown>,
    opts: {
      inherentlySafe: boolean;
      description: string | null;
      autoApprovalCriteria: string | null;
    },
  ): AutoApprovalContext | null {
    if (this.#evaluator === undefined) return null;
    if (
      !ToolApprovalPolicy.consultsAutoMode(this.#policy, toolName, {
        inherentlySafe: opts.inherentlySafe,
      })
    )
      return null;
    const criteria = this.#criteria;
    const criteriaSet = AutoApprovalCriteria.forTool(criteria, toolName, opts.autoApprovalCriteria);
    const name = AutoApprovalCriteria.setNameFor(criteria, toolName, opts.autoApprovalCriteria);
    if (criteriaSet === null) {
      if (name !== null) {
        log.warn(
          `Auto mode skipped tool '${toolName}': the criteria set '${name}' it is assigned to is not registered ` +
            "(or carries no rules), so the call went to the human gate.",
        );
      }
      return null;
    }
    return {
      tool_name: toolName,
      tool_input: toolInput,
      inherently_safe: opts.inherentlySafe,
      tool_description: opts.description,
      criteria,
      criteria_version: this.#criteriaVersion,
      criteria_set: criteriaSet,
      criteria_set_name: name ?? "",
      thresholds: AutoApprovalCriteria.thresholdsForSet(criteria, criteriaSet),
    };
  }

  async #runAutoModeEvaluator(
    ctx: AutoApprovalContext,
    toolId: string,
    stream: TurnStreamContext,
  ): Promise<AutoApprovalDecision | null> {
    const evaluatorFn = this.#evaluator!;
    const evaluationId = uuid4().replaceAll("-", "");
    const evaluator = evaluatorFn.evaluatorLabel || evaluatorFn.name || "auto_mode_evaluator";
    const ids = {
      tool_id: toolId,
      tool_name: ctx.tool_name,
      evaluation_id: evaluationId,
      evaluator,
    };
    const close = (event: AgentStreamItem) => this.pub(stream.turn_id, stream.turn_number, event);
    close({ type: "auto_approval_evaluation_started", ...ids });

    const scope = new CancellationScope();
    let outcome: { ok: AutoApprovalDecision } | { err: unknown } | undefined;
    const running = scope
      .run(() => evaluatorFn(ctx))
      .then(
        (ok) => (outcome = { ok }),
        (err: unknown) => (outcome = { err }),
      );
    const settled = () => this.#isApprovalResolved(toolId) || this.#closed;
    await condition(() => outcome !== undefined || settled());
    if (settled()) {
      const reached =
        outcome !== undefined && "ok" in outcome
          ? (WIRE_VERDICT[outcome.ok?.verdict] ?? null)
          : null;
      scope.cancel();
      await running;
      close({ type: "auto_approval_evaluation_superseded", ...ids, verdict: reached });
      return null;
    }
    if (
      outcome !== undefined &&
      "ok" in outcome &&
      !Object.hasOwn(WIRE_VERDICT, String(outcome.ok?.verdict))
    ) {
      outcome = {
        err: new TypeError(
          `auto mode evaluator '${evaluator}' returned ${JSON.stringify(outcome.ok)}; it must return an AutoApprovalDecision`,
        ),
      };
    }
    if (outcome !== undefined && "err" in outcome) {
      log.error(
        `auto mode evaluator '${evaluator}' raised for tool '${ctx.tool_name}'; escalating to the human gate`,
      );
      close({ type: "auto_approval_evaluation_error", ...ids, message: errorText(outcome.err) });
      return null;
    }
    const decision = (outcome as { ok: AutoApprovalDecision }).ok;
    close({
      type: "auto_approval_evaluation_ended",
      ...ids,
      verdict: WIRE_VERDICT[decision.verdict],
      reason: decision.reason ?? null,
      details: decision.details ?? {},
    });
    return decision;
  }

  #validateToolApproval(raw: unknown): void {
    const decision = ToolApprovalDecisionSchema.parse(raw);
    if (this.#closed) {
      // The gate denies every pending call on close; a decision racing the close must not run one.
      throw ApplicationFailure.create({
        type: "AgentClosed",
        nonRetryable: true,
        message: "the agent is closed; its pending tool calls were denied",
      });
    }
    const entry = this.#approvals.get(decision.tool_id);
    if (entry === undefined) {
      throw ApplicationFailure.create({
        type: "UnknownToolApproval",
        nonRetryable: true,
        message: `no pending tool approval for tool_id='${decision.tool_id}'`,
      });
    }
    if (entry.status !== "pending") {
      throw ApplicationFailure.create({
        type: "ToolApprovalAlreadyResolved",
        nonRetryable: true,
        message: `tool approval for tool_id='${decision.tool_id}' is already ${entry.status}`,
      });
    }
  }

  #handleToolApproval(raw: unknown): ToolApprovalResult {
    const decision: ToolApprovalDecision = ToolApprovalDecisionSchema.parse(raw);
    const entry = this.#approvals.get(decision.tool_id)!;
    this.#resolveAndPublish(
      decision.tool_id,
      decision.approved,
      decision.reason,
      decision.remember,
    );
    if (decision.approved && decision.remember) {
      this.setApprovalPolicy(ToolApprovalPolicy.withToolAllowed(this.#policy, entry.tool_name));
    }
    return { tool_id: decision.tool_id, accepted: true };
  }

  #resolveAndPublish(
    toolId: string,
    approved: boolean,
    reason: string | null,
    remember = false,
  ): void {
    const entry = this.#approvals.get(toolId)!;
    entry.status = approved ? "approved" : "denied";
    entry.reason = reason;
    entry.remember = remember;
    this.#publishApprovalResolved(entry);
  }

  #publishApprovalResolved(entry: ApprovalEntry): void {
    this.pub(
      entry.turn_id,
      entry.turn_number,
      {
        type: "tool_approval_resolved",
        tool_id: entry.tool_id,
        tool_name: entry.tool_name,
        approved: entry.status === "approved",
        reason: entry.reason,
        remember: entry.remember,
      },
      entry.message_id,
    );
  }

  // -- callback tools -------------------------------------------------------------------------

  /** @internal The generic body of a callback tool: park until a client provides the result. */
  async awaitCallbackResult(args: {
    toolId: string;
    toolName: string;
    toolInput: Record<string, unknown>;
    output: Schema;
    outputSchema: Record<string, unknown>;
    timeout: string | number | undefined;
  }): Promise<unknown> {
    const ctx = this.currentStreamContext;
    if (ctx === null)
      throw new Error(
        `callback tool '${args.toolName}' has no active turn to await a result against`,
      );
    const entry: CallbackEntry = {
      tool_id: args.toolId,
      tool_name: args.toolName,
      tool_input: args.toolInput,
      output_schema: args.outputSchema,
      output: args.output,
      turn_number: ctx.turn_number,
      turn_id: ctx.turn_id,
      message_id: ctx.message_id,
      status: "pending",
      outcome: "ok",
      result: null,
      error: null,
    };
    this.#callbacks.set(args.toolId, entry);
    this.pub(ctx.turn_id, ctx.turn_number, {
      type: "callback_requested",
      tool_id: args.toolId,
      tool_name: args.toolName,
      tool_input: args.toolInput,
      output_schema: args.outputSchema,
    });
    const done = () => entry.status === "resolved" || this.#closed;
    const inTime =
      args.timeout === undefined ? (await condition(done), true) : await condition(done, args.timeout);
    if (entry.status === "pending") {
      entry.status = "resolved";
      entry.outcome = "timeout";
      entry.error = !inTime
        ? "callback timed out before a result arrived"
        : this.#closed
          ? "agent closed before the callback result arrived"
          : "callback unresolved";
      this.#publishCallbackResolved(entry);
    }
    if (entry.outcome === "ok") return entry.result;
    throw new CallbackToolError(args.toolName, entry.error);
  }

  #validateProvideCallbackResult(raw: unknown): void {
    const result = CallbackResultSchema.parse(raw);
    assertPortableJson(result.result, `Result for callback '${result.tool_id}'`);
    const entry = this.#callbacks.get(result.tool_id);
    if (entry === undefined) {
      throw ApplicationFailure.create({
        type: "UnknownCallback",
        nonRetryable: true,
        message: `no pending callback for tool_id='${result.tool_id}'`,
      });
    }
    if (entry.status !== "pending") {
      throw ApplicationFailure.create({
        type: "CallbackAlreadyResolved",
        nonRetryable: true,
        message: `callback for tool_id='${result.tool_id}' is already ${entry.status}`,
      });
    }
    if (result.error === null) {
      const parsed = validate(entry.output, result.result);
      if (!parsed.ok) {
        throw ApplicationFailure.create({
          type: "MalformedCallbackResult",
          nonRetryable: true,
          message: `callback result for tool_id='${result.tool_id}' does not match the tool's declared output type. Validation error: ${parsed.message}`,
          details: [{ tool_id: result.tool_id, error: parsed.message }],
        });
      }
    }
  }

  #handleProvideCallbackResult(raw: unknown): CallbackResultAck {
    const result: CallbackResult = CallbackResultSchema.parse(raw);
    const entry = this.#callbacks.get(result.tool_id)!;
    entry.status = "resolved";
    if (result.error !== null) {
      entry.outcome = "error";
      entry.error = result.error;
    } else {
      entry.outcome = "ok";
      const parsed = validate(entry.output, result.result);
      entry.result = parsed.ok ? parsed.value : result.result;
    }
    this.#publishCallbackResolved(entry);
    return { tool_id: result.tool_id, accepted: true };
  }

  #publishCallbackResolved(entry: CallbackEntry): void {
    this.pub(
      entry.turn_id,
      entry.turn_number,
      {
        type: "callback_resolved",
        tool_id: entry.tool_id,
        tool_name: entry.tool_name,
        outcome: entry.outcome,
        error: entry.error,
      },
      entry.message_id,
    );
  }

  // -- subagents ------------------------------------------------------------------------------

  /** Start a child agent; returns its short, tree-unique handle (also its agent_id). */
  async startSubagent(
    agentKey: string,
    workflowType: string,
    taskQueue: string,
    { config = {}, data }: { config?: Partial<AgentConfig>; data?: unknown } = {},
  ): Promise<string> {
    let handle = `${this.agentId}-${shortHex(AGENT_ID_LENGTH)}`;
    while (this.#subagents.has(handle)) handle = `${this.agentId}-${shortHex(AGENT_ID_LENGTH)}`;
    const workflowId = `${agentKey}-subagent-${uuid4()}`;
    const childConfig = { ...config, agent_id: handle };
    await startChild(workflowType, {
      args: data === undefined ? [childConfig] : [childConfig, data],
      workflowId,
      taskQueue,
      // A subagent is owned by its parent and never outlives it: the server terminates it when
      // the parent closes for any reason before stopSubagent. Pinned rather than left to the
      // SDK's default, as in Python.
      parentClosePolicy: ParentClosePolicy.TERMINATE,
    });
    this.#subagents.set(handle, {
      handle,
      workflow_id: workflowId,
      agent_key: agentKey,
      last_consumed_offset: 0,
      next_ticket: 0,
      serving: 0,
    });
    this.publish({
      type: "subagent_started",
      subagent_id: handle,
      agent_key: agentKey,
      workflow_id: workflowId,
    });
    return handle;
  }

  async stopSubagent(handle: string): Promise<void> {
    const inst = this.#subagent(handle);
    await getExternalWorkflowHandle(inst.workflow_id).signal(CLOSE_SIGNAL);
    this.publish({
      type: "subagent_stopped",
      subagent_id: inst.handle,
      agent_key: inst.agent_key,
      workflow_id: inst.workflow_id,
    });
    this.#subagents.delete(handle);
  }

  /** Send one message to a subagent and await its reply. Turns to one subagent run FIFO. */
  async runSubagentTurn(
    handle: string,
    msgType: string,
    payload: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const inst = this.#subagent(handle);
    const ticket = inst.next_ticket++;
    await condition(() => inst.serving === ticket || this.#closed);
    if (inst.serving !== ticket) {
      throw ApplicationFailure.create({
        type: "AgentClosed",
        nonRetryable: true,
        message: `agent closed before subagent '${handle}' turn ${ticket} could run`,
      });
    }
    try {
      const ctx = this.currentStreamContext;
      if (ctx === null) {
        throw ApplicationFailure.create({
          type: "NoActiveTurn",
          nonRetryable: true,
          message: "run_subagent_turn called with no active turn",
        });
      }
      const { run_subagent_turn } = proxyActivities<{
        run_subagent_turn(input: RunSubagentTurnInput): Promise<SubagentTurnResult>;
      }>({
        // A child turn can outlast any start-to-close, so an attempt that times out resumes from
        // its heartbeat on the next one; the attempts are uncapped, as in Python.
        startToCloseTimeout: DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT,
        heartbeatTimeout: DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT,
      });
      const input: RunSubagentTurnInput = {
        child_workflow_id: inst.workflow_id,
        type: msgType,
        payload,
        from_offset: inst.last_consumed_offset,
        handle: inst.handle,
        agent_key: inst.agent_key,
        parent_stream_context: ctx,
      };
      let result: SubagentTurnResult;
      try {
        result = await run_subagent_turn(input);
      } catch (e) {
        const cause = e instanceof ActivityFailure ? e.cause : undefined;
        if (
          cause instanceof ApplicationFailure &&
          (cause.type === "SubagentTurnError" || cause.type === "SubagentNoReply")
        ) {
          this.#publishReplyReceived(inst, msgType, acceptedTurnFromError(cause), "error");
        }
        throw e;
      }
      inst.last_consumed_offset = result.consumed_offset;
      this.#publishReplyReceived(inst, msgType, result.turn_number, "ok");
      return result.output;
    } finally {
      inst.serving += 1;
    }
  }

  #publishReplyReceived(
    inst: SubagentInstance,
    handler: string,
    subagentTurn: number,
    outcome: "ok" | "error",
  ): void {
    this.publish({
      type: "subagent_reply_received",
      subagent_id: inst.handle,
      agent_key: inst.agent_key,
      workflow_id: inst.workflow_id,
      handler,
      subagent_turn: subagentTurn,
      outcome,
    });
  }

  #subagent(handle: string): SubagentInstance {
    const inst = this.#subagents.get(handle);
    if (inst === undefined) {
      const known = [...this.#subagents.keys()].sort();
      throw ApplicationFailure.create({
        type: "UnknownSubagent",
        nonRetryable: true,
        message: `Unknown subagent '${handle}'. Known subagents: ${JSON.stringify(known)}.`,
        details: [{ handle, known }],
      });
    }
    return inst;
  }

  // -- plumbing -------------------------------------------------------------------------------

  #agentInterface(): AcceptedFunction[] {
    return Object.entries(this.#handlers).map(([name, h]) => ({
      name,
      description: h.description,
      parameters: toJsonSchema(h.input),
      output: toJsonSchema(h.output),
      mid_turn: h.midTurn,
      model_callable: h.modelCallable,
    }));
  }

  #publishStateEvent(event: StateEvent): void {
    const ctx = this.currentStreamContext;
    this.pub(ctx?.turn_id ?? "", ctx?.turn_number ?? 0, event);
  }

  /** @internal Stamp the envelope and publish. `messageId` defaults to the ambient participant. */
  // Replaces WorkflowStream's own publish-signal handler, with the same per-publisher dedup. Model
  // activities stream their SDK's raw events to a per-call raw topic; they are translated here
  // and published to turn_events, so every reader (either language, the Nexus adapter) sees the
  // harness events, and the raw events themselves are not kept. Translation is pure and uses the
  // workflow clock, so it replays deterministically. Dedup uses the stream's own maps, so
  // `stream.getState()` carries them across a continue-as-new.
  #onPublish(input: PublishInput): void {
    if (input.publisher_id) {
      const s = this.#stream;
      if (input.sequence <= (s.publisherSequences[input.publisher_id] ?? 0)) return;
      s.publisherSequences[input.publisher_id] = input.sequence;
      s.publisherLastSeen[input.publisher_id] = Date.now() / 1000;
    }
    for (const entry of input.items) {
      const payload = decodePayloadWire(entry.data);
      if (parseRawTopic(entry.topic) === null) this.stream.topic(entry.topic).publish(payload);
      else this.#publishRaw(entry.topic, defaultPayloadConverter.fromPayload(payload));
    }
  }

  #publishRaw(topic: string, data: unknown): void {
    for (const event of this.#rawEvents.translate(topic, data)) this.#events.publish(event);
  }

  pub(
    turnId: string,
    turnNumber: number,
    event: AgentStreamItem,
    messageId: string | null | typeof AMBIENT = AMBIENT,
  ): void {
    this.#events.publish({
      agent_id: this.agentId,
      turn_id: turnId,
      turn_number: turnNumber,
      message_id: messageId === AMBIENT ? (messageIdStore.getStore() ?? null) : messageId,
      timestamp: Date.now() / 1000,
      event,
    });
  }
}
