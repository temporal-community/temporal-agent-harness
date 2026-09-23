// Frame builders and a scripted transport for the tests.

import type { AgentSseFrame } from "../src/frames.ts";
import type {
  AgentStatusSnapshot,
  SessionTransport,
  SubmitMessageResponse,
  ToolApprovalDecision,
  WorkflowExecutionState
} from "../src/transport.ts";

export interface Envelope {
  agent_id?: string;
  message_id?: string | null;
  turn_number?: number;
}

/** Builds frames with increasing timestamps and resume offsets. */
export class Frames {
  #time = 0;
  #offset = 0;

  make(event: string, data: Record<string, unknown> = {}, env: Envelope = {}): AgentSseFrame {
    const turn = env.turn_number ?? 1;
    return {
      event,
      data: {
        type: event,
        agent_id: env.agent_id ?? "root",
        turn_id: `turn-${turn}`,
        turn_number: turn,
        message_id: env.message_id ?? null,
        timestamp: ++this.#time,
        resume_offset: ++this.#offset,
        ...data
      }
    } as unknown as AgentSseFrame;
  }

  /** One message's whole life in its own turn: admitted, run, replied, ended. */
  exchange(id: string, text: string, turn: number): AgentSseFrame[] {
    const env = { message_id: id, turn_number: turn };
    return [
      this.make("message_accepted", { handler: "ask", payload: { text: "hi" }, disposition: "opened" }, env),
      this.make("turn_started", {}, { turn_number: turn }),
      this.make("message_handler_start", {}, env),
      this.make("reply_delta", { text }, env),
      this.make("message_handler_end", { output: { text } }, env),
      this.make("turn_end", {}, { turn_number: turn })
    ];
  }
}

type Step = AgentSseFrame | Error | { hold: Promise<void> };

/** A transport whose attaches play back scripts, one per call, in order. A script that runs out
 *  ends the attach; an `Error` step throws; a `hold` step waits until released. */
export class ScriptedTransport implements SessionTransport {
  readonly attaches: number[] = [];
  readonly submitted: Array<{ type: string; payload: unknown }> = [];
  readonly callbackResults: Array<{ toolId: string; outcome: unknown }> = [];
  readonly approvals: Array<{ toolId: string; decision: ToolApprovalDecision }> = [];
  /** `workflow:toolId` for every approval and callback result, in the order they were sent. */
  readonly routed: string[] = [];
  readonly #scripts: Step[][];
  status: AgentStatusSnapshot = { current_turn: 0, turn_active: false, pending_turns: [] };
  closed = false;
  submitReply: (n: number) => SubmitMessageResponse | Error = (n) => ({
    turn_number: n,
    turn_id: `turn-${n}`,
    message_id: `m${n}`,
    accepted_offset: 0,
    disposition: "opened"
  });

  constructor(scripts: Step[][] = []) {
    this.#scripts = scripts;
  }

  script(steps: Step[]): void {
    this.#scripts.push(steps);
  }

  async *attach(_sessionId: string, fromOffset: number, signal: AbortSignal): AsyncIterable<AgentSseFrame> {
    this.attaches.push(fromOffset);
    const steps = this.#scripts.shift() ?? [];
    for (const step of steps) {
      if (signal.aborted) return;
      if (step instanceof Error) throw step;
      if ("hold" in step) {
        await Promise.race([step.hold, aborted(signal)]);
        continue;
      }
      await Promise.resolve();
      yield step;
    }
  }

  async submitMessage(_sessionId: string, message: { type: string; payload: unknown }): Promise<SubmitMessageResponse> {
    this.submitted.push(message);
    const reply = this.submitReply(this.submitted.length);
    if (reply instanceof Error) throw reply;
    return reply;
  }

  async approveTool(sessionId: string, toolId: string, decision: ToolApprovalDecision): Promise<void> {
    this.approvals.push({ toolId, decision });
    this.routed.push(`${sessionId}:${toolId}`);
  }

  async provideCallbackResult(sessionId: string, toolId: string, outcome: unknown): Promise<void> {
    this.callbackResults.push({ toolId, outcome });
    this.routed.push(`${sessionId}:${toolId}`);
  }

  async agentStatus(): Promise<AgentStatusSnapshot> {
    return this.status;
  }

  async workflowStatus(sessionId: string): Promise<WorkflowExecutionState> {
    return { workflow_id: sessionId, execution_status: this.closed ? "COMPLETED" : "RUNNING", closed: this.closed };
  }

  async closeSession(): Promise<void> {
    this.closed = true;
  }
}

function aborted(signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
}

export function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => (resolve = r));
  return { promise, resolve };
}

/** Wait until `check` holds, or fail after `ms`. */
export async function until(check: () => boolean, ms = 2_000): Promise<void> {
  const deadline = Date.now() + ms;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out waiting for a condition");
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
}
