// AgentSessionCore: one agent session's connection, projection and actions, independent of
// any UI framework. A framework binding supplies a `SessionState` — plain fields plus three
// batch updates — whose fields it makes reactive; the core writes to it once per flush.

import type { AgentSseFrame } from "./frames.ts";
import type { HarnessMessage, ToolPart } from "./messages.ts";
import { SessionProjection, type AgentInfo } from "./projection.ts";
import type { AgentSchema, HandlerInput, HandlerName, UntypedAgent } from "./schema.ts";
import {
  HttpTransport,
  type SessionTransport,
  type SubmitMessageResponse,
  type ToolApprovalDecision
} from "./transport.ts";
import type { AgentUpdate, AgentView } from "./views.ts";

/**
 * - `connecting`: the first attach has not delivered anything yet.
 * - `live`: attached and receiving.
 * - `idle`: caught up with an idle agent, and watching for new work.
 * - `reconnecting`: the stream dropped and is being retried.
 * - `closed`: the agent's workflow has finished; nothing more will arrive.
 * - `error`: retries were exhausted. Sending a message tries again.
 * - `stopped`: nothing is observing the session, so it holds no connection.
 */
export type ConnectionStatus =
  | "stopped"
  | "connecting"
  | "live"
  | "idle"
  | "reconnecting"
  | "closed"
  | "error";

/** The fields a session publishes, and how the core writes them. A binding makes the fields
 *  reactive; the core never mutates an array or object it has already handed over. Apply an
 *  update with `applyAgentUpdate`; `rootView` reads the root agent's view out of `agents`. */
export interface SessionState<A extends AgentSchema> {
  connection: ConnectionStatus;
  /** The last connection or action failure, until `clearError()`. */
  error: Error | undefined;
  /** The agent the session was opened for, once the stream has named it. */
  rootAgentId: string | null;
  /** Every agent in the session's tree — the root and its subagents at any depth — by
   *  `agent_id`. The root's is typed `AgentView<A>`; read it with `rootView`. */
  agents: Readonly<Record<string, AgentView>>;
  /** Messages sent from this client that the server has not admitted yet, and failed sends. */
  pending: readonly HarnessMessage<A>[];
  /** Every frame received, from every agent, for views that inspect the raw stream. */
  frames: readonly AgentSseFrame[];
  /** Apply one flush's changes, one entry per agent that changed. */
  updateAgents(updates: ReadonlyArray<readonly [string, AgentUpdate]>): void;
  appendFrames(frames: readonly AgentSseFrame[]): void;
}

/** A callback tool call waiting on this client. */
export interface ToolCall {
  toolId: string;
  toolName: string;
  /** The agent that made the call: the root, or one of its subagents. */
  agentId: string;
  input: Record<string, unknown>;
  /** JSON Schema of the result the tool expects back. */
  outputSchema: Record<string, unknown>;
}

export interface AgentSessionOptions<A extends AgentSchema> {
  sessionId: string;
  transport?: SessionTransport;
  /** Callback tools this client fulfils, by tool name: return the result, or throw to report the
   *  call failed. Each is called once per call still waiting when it is seen, including one that
   *  was waiting before this client attached. A tool with no entry is left for someone to answer
   *  with `provideToolOutput` or `provideToolError`. */
  callbackTools?: Readonly<Record<string, (call: ToolCall) => unknown>>;
  /** A message sent from this client finished: its handler returned or raised. */
  onFinish?: (message: HarnessMessage<A>) => void;
  onError?: (error: Error) => void;
  /** Waits before each retry of a dropped stream; the budget resets whenever it delivers. */
  reconnectBackoffMs?: readonly number[];
  /** While the agent is idle, how often to ask whether another client gave it work. 0 turns
   *  this off, leaving sends from this client as the only thing that re-attaches. */
  idlePollMs?: number;
  /** Runs a flush of received frames into the state. Defaults to the next animation frame. */
  schedule?: (flush: () => void) => void;
}

const DEFAULT_BACKOFF_MS = [500, 1_000, 2_000, 4_000, 8_000, 8_000, 8_000];
const DEFAULT_IDLE_POLL_MS = 5_000;

export class AgentSessionCore<A extends AgentSchema = UntypedAgent> {
  readonly sessionId: string;
  readonly #state: SessionState<A>;
  readonly #transport: SessionTransport;
  readonly #options: AgentSessionOptions<A>;
  readonly #backoff: readonly number[];
  readonly #idlePollMs: number;
  readonly #schedule: (flush: () => void) => void;
  readonly #projection = new SessionProjection();
  readonly #frameKeys = new Set<string>();
  readonly #sentIds = new Set<string>();
  readonly #finished = new Set<string>();
  readonly #dispatchedCalls = new Set<string>();
  #buffer: AgentSseFrame[] = [];
  #flushScheduled = false;
  #resumeOffset = 0;
  #observed = false;
  #run: AbortController | null = null;
  #wake: (() => void) | null = null;
  #submits: Promise<unknown> = Promise.resolve();
  #localIds = 0;

  constructor(options: AgentSessionOptions<A>, state: SessionState<A>) {
    this.sessionId = options.sessionId;
    this.#options = options;
    this.#state = state;
    this.#transport = options.transport ?? new HttpTransport();
    this.#backoff = options.reconnectBackoffMs ?? DEFAULT_BACKOFF_MS;
    this.#idlePollMs = options.idlePollMs ?? DEFAULT_IDLE_POLL_MS;
    this.#schedule = options.schedule ?? nextFrame;
  }

  get state(): SessionState<A> {
    return this.#state;
  }

  /** Open the connection, if it is not open. A binding calls this when something starts
   *  observing the session. */
  start(): void {
    this.#observed = true;
    this.#ensureLive();
  }

  /** Drop the connection. A binding calls this when nothing observes the session any more. */
  stop(): void {
    this.#observed = false;
    this.#run?.abort();
    this.#run = null;
    this.#flush();
    this.#state.connection = "stopped";
  }

  /** Send a message to one of the agent's handlers. Resolves with the server's acceptance, or
   *  null if the submit failed, in which case the message stays in `pending` with the error. */
  async sendMessage<H extends HandlerName<A>>(
    handler: H,
    payload: HandlerInput<A, H>
  ): Promise<SubmitMessageResponse | null> {
    const localId = `local-${++this.#localIds}`;
    this.#projection.addSending({
      id: localId,
      handler,
      input: payload as Record<string, unknown>,
      output: null,
      status: "sending",
      disposition: null,
      turnNumber: null,
      parts: [],
      error: null,
      acceptedAt: null,
      startedAt: null,
      endedAt: null
    });
    this.#scheduleFlush();

    // Submits go out one at a time, in the order they were sent.
    const submitted = this.#submits.then(() =>
      this.#transport.submitMessage(this.sessionId, { type: handler, payload })
    );
    this.#submits = submitted.catch(() => undefined);
    try {
      const reply = await submitted;
      this.#sentIds.add(reply.message_id);
      this.#projection.admitSending(localId, reply.message_id);
      this.#flush();
      // Only after the submit returns: once admitted, the message is visible to the server's
      // idle check, so a stream opened now cannot end before it.
      this.#ensureLive();
      return reply;
    } catch (error) {
      this.#projection.failSending(localId, messageOf(error));
      this.#flush();
      this.#fail(error);
      return null;
    }
  }

  /** Drop a failed send from `pending`. */
  dismiss(localId: string): void {
    this.#projection.dismissSending(localId);
    this.#flush();
  }

  /** Approve or deny a gated tool call, in the root agent or any subagent. The resolution
   *  itself arrives on the stream. */
  async respondToApproval(toolId: string, decision: ToolApprovalDecision): Promise<boolean> {
    return this.#act(() => this.#transport.approveTool(this.#workflowOf(toolId), toolId, decision));
  }

  /** Return a callback tool's result. The server validates it against the tool's output type. */
  async provideToolOutput(toolId: string, result: unknown): Promise<boolean> {
    return this.#act(() =>
      this.#transport.provideCallbackResult(this.#workflowOf(toolId), toolId, { result })
    );
  }

  /** Report that this client could not fulfil a callback tool call. */
  async provideToolError(toolId: string, error: string): Promise<boolean> {
    return this.#act(() =>
      this.#transport.provideCallbackResult(this.#workflowOf(toolId), toolId, { error })
    );
  }

  /** A tool call is answered at the workflow of the agent that made it; a subagent's gates
   *  are its own, not its parent's. */
  #workflowOf(toolId: string): string {
    return this.#projection.toolAgent(toolId)?.workflowId ?? this.sessionId;
  }

  /** Stop the agent: it finishes its turn loop and auto-denies anything pending. */
  async closeSession(): Promise<boolean> {
    return this.#act(() => this.#transport.closeSession(this.sessionId));
  }

  clearError(): void {
    this.#state.error = undefined;
  }

  // -- the connection --------------------------------------------------------

  #ensureLive(): void {
    if (!this.#observed) return;
    if (this.#run) {
      this.#wake?.();
      return;
    }
    const run = new AbortController();
    this.#run = run;
    void this.#connect(run.signal).finally(() => {
      if (this.#run === run) this.#run = null;
    });
  }

  async #connect(signal: AbortSignal): Promise<void> {
    let attempt = 0;
    this.#state.connection = "connecting";
    while (!signal.aborted) {
      let delivered = false;
      try {
        for await (const frame of this.#transport.attach(this.sessionId, this.#resumeOffset, signal)) {
          if (signal.aborted) return;
          if (!delivered) {
            delivered = true;
            this.#state.connection = "live";
          }
          this.#receive(frame);
        }
      } catch (error) {
        if (signal.aborted) return;
        this.#flush();
        if (delivered) attempt = 0;
        if (!(await this.#retry(attempt++, signal, error))) return;
        continue;
      }
      this.#flush();
      if (signal.aborted) return;
      if (delivered) attempt = 0;
      if (await this.#workflowClosed(signal)) {
        this.#state.connection = "closed";
        return;
      }
      // The server ends an attach once the agent is idle and caught up, so a stream that
      // ends with a turn still open was cut off.
      if (this.#projection.root?.agentStatus === "busy") {
        if (!(await this.#retry(attempt++, signal, null))) return;
        continue;
      }
      this.#state.connection = "idle";
      await this.#waitForWork(signal);
    }
  }

  async #retry(attempt: number, signal: AbortSignal, error: unknown): Promise<boolean> {
    const wait = this.#backoff[attempt];
    if (wait === undefined) {
      this.#state.connection = "error";
      this.#fail(error ?? new Error("the event stream kept closing mid-turn"));
      return false;
    }
    this.#state.connection = "reconnecting";
    await this.#sleep(wait, signal);
    return !signal.aborted;
  }

  async #workflowClosed(signal: AbortSignal): Promise<boolean> {
    try {
      return (await this.#transport.workflowStatus(this.sessionId, signal)).closed;
    } catch {
      return false;
    }
  }

  /** Resolve when there may be something new to read: a send from this client wakes it, and
   *  a poll of the agent's status notices work another client gave it. */
  #waitForWork(signal: AbortSignal): Promise<void> {
    return new Promise((resolve) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const done = (): void => {
        clearTimeout(timer);
        signal.removeEventListener("abort", done);
        if (this.#wake === done) this.#wake = null;
        resolve();
      };
      this.#wake = done;
      signal.addEventListener("abort", done, { once: true });
      if (this.#idlePollMs <= 0) return;
      const poll = (): void => {
        timer = setTimeout(async () => {
          try {
            const status = await this.#transport.agentStatus(this.sessionId, signal);
            const newWork =
              status.turn_active ||
              status.pending_turns.length > 0 ||
              status.current_turn > (this.#projection.root?.lastTurnNumber ?? 0);
            if (newWork) return done();
          } catch {
            // A failed poll is not news; the next one will ask again.
          }
          if (!signal.aborted && this.#wake === done) poll();
        }, this.#idlePollMs);
      };
      poll();
    });
  }

  /** Sleep before a retry; a send wakes it early. */
  #sleep(ms: number, signal: AbortSignal): Promise<void> {
    return new Promise((resolve) => {
      const done = (): void => {
        clearTimeout(timer);
        signal.removeEventListener("abort", done);
        if (this.#wake === done) this.#wake = null;
        resolve();
      };
      const timer = setTimeout(done, ms);
      this.#wake = done;
      signal.addEventListener("abort", done, { once: true });
    });
  }

  // -- frames ----------------------------------------------------------------

  #receive(frame: AgentSseFrame): void {
    // A re-attach resumes from a root-stream offset that several subagent frames share, so
    // some frames arrive twice.
    const key = frameKey(frame);
    if (this.#frameKeys.has(key)) return;
    this.#frameKeys.add(key);
    this.#resumeOffset = Math.max(this.#resumeOffset, frame.data.resume_offset);
    this.#buffer.push(frame);
    this.#projection.apply(frame);
    this.#scheduleFlush();
  }

  #scheduleFlush(): void {
    if (this.#flushScheduled) return;
    this.#flushScheduled = true;
    this.#schedule(() => this.#flush());
  }

  #flush(): void {
    this.#flushScheduled = false;
    const state = this.#state;
    if (this.#buffer.length > 0) {
      state.appendFrames(this.#buffer);
      this.#buffer = [];
    }
    const changes = this.#projection.takeChanges();
    if (state.rootAgentId !== this.#projection.rootAgentId) {
      state.rootAgentId = this.#projection.rootAgentId;
    }
    if (changes.agents.length > 0) {
      state.updateAgents(
        changes.agents.map(([id, c]) => [
          id,
          {
            info: c.info,
            messages: c.messages,
            states: c.states?.map(([stateId, doc]) => [stateId, doc.value] as const),
            agentStatus: c.agentStatus
          }
        ])
      );
    }
    if (changes.pending !== null) state.pending = changes.pending as readonly HarnessMessage<A>[];

    const rootChanges = changes.agents.find(([id]) => id === this.#projection.rootAgentId)?.[1];
    for (const [, message] of rootChanges?.messages ?? []) {
      if (message.status !== "done" && message.status !== "error") continue;
      if (!this.#sentIds.has(message.id) || this.#finished.has(message.id)) continue;
      this.#finished.add(message.id);
      this.#options.onFinish?.(message as HarnessMessage<A>);
    }
    for (const { part, agent } of this.#projection.awaitingClient()) this.#dispatchCall(part, agent);
  }

  /** Whether `callbackTools` answers this tool, so a view of waiting calls can leave it out. */
  handlesCallback(toolName: string): boolean {
    return this.#callbackTool(toolName) !== undefined;
  }

  #callbackTool(toolName: string): ((call: ToolCall) => unknown) | undefined {
    const tools = this.#options.callbackTools;
    return tools && Object.hasOwn(tools, toolName) ? tools[toolName] : undefined;
  }

  #dispatchCall(part: ToolPart, agent: AgentInfo): void {
    const handler = this.#callbackTool(part.toolName);
    if (!handler || this.#dispatchedCalls.has(part.toolId)) return;
    this.#dispatchedCalls.add(part.toolId);
    const call: ToolCall = {
      toolId: part.toolId,
      toolName: part.toolName,
      agentId: agent.agentId,
      input: part.input,
      outputSchema: part.callback?.outputSchema ?? {}
    };
    void (async () => {
      try {
        const result = await handler(call);
        await this.provideToolOutput(part.toolId, result);
      } catch (error) {
        await this.provideToolError(part.toolId, messageOf(error));
      }
    })();
  }

  async #act(action: () => Promise<void>): Promise<boolean> {
    try {
      await action();
      this.#ensureLive();
      return true;
    } catch (error) {
      this.#fail(error);
      return false;
    }
  }

  #fail(error: unknown): void {
    const failure = error instanceof Error ? error : new Error(messageOf(error));
    this.#state.error = failure;
    this.#options.onError?.(failure);
  }
}

/** Identifies a frame so a redelivery can be recognized: by its log offset when the server
 *  reports one, else by its content, which is unique to it in practice. */
export function frameKey(frame: AgentSseFrame): string {
  const data = frame.data as unknown as Record<string, unknown>;
  if (typeof data.event_offset === "number" && data.event_offset >= 0 && "agent_id" in data) {
    return `${String(data.agent_id)}|${data.event_offset}`;
  }
  const { resume_offset: _resume, event_offset: _event, ...identity } = data;
  return `${frame.event}|${JSON.stringify(identity)}`;
}

function nextFrame(flush: () => void): void {
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => flush());
  else setTimeout(flush, 0);
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
