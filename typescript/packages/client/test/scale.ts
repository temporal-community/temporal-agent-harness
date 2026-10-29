// Feeds a long synthetic session through AgentSessionCore in flushes of 24 frames and times
// each flush, and separately a read of the waiting tool calls after it. `node
// dist/test/scale.js` prints the table; scale.test.ts asserts the per-flush cost does not grow
// with the session.

import type { AgentSseFrame } from "../src/frames.ts";
import { PlainSessionState } from "../src/plainState.ts";
import { AgentSessionCore } from "../src/session.ts";
import type { AgentStatusSnapshot, SessionTransport } from "../src/transport.ts";
import { waitingCalls } from "../src/views.ts";
import { Frames } from "./support.ts";

const BATCH = 24;

/** At least `count` frames, in messages of ~24: a reply streamed in deltas around two tool
 *  calls, one of them left waiting on an approval so the waiting lists are never empty. */
export function syntheticFrames(count: number): AgentSseFrame[] {
  const f = new Frames();
  const frames: AgentSseFrame[] = [];
  for (let m = 0; frames.length < count; m++) {
    const id = `m${m}`;
    const turn = m + 1;
    const env = { message_id: id, turn_number: turn };
    frames.push(
      f.make("message_accepted", { handler: "ask", payload: { text: "hi" }, disposition: "opened" }, env),
      f.make("turn_started", {}, { turn_number: turn }),
      f.make("message_handler_start", {}, env)
    );
    for (let t = 0; t < 2; t++) {
      const tool = { tool_id: `${id}-t${t}`, tool_name: "lookup" };
      for (let d = 0; d < 4; d++) frames.push(f.make("reply_delta", { text: `chunk ${d} ` }, env));
      frames.push(f.make("tool_requested", { ...tool, tool_input: { q: t } }, env));
      if (t === 1 && m % 50 === 0) {
        frames.push(f.make("tool_approval_requested", { ...tool, tool_input: { q: t } }, env));
        continue;
      }
      frames.push(
        f.make("tool_start", { ...tool, tool_input: { q: t } }, env),
        f.make("tool_end", { ...tool, tool_output: { ok: true } }, env)
      );
    }
    frames.push(
      f.make("message_handler_end", { output: { text: "done" } }, env),
      f.make("turn_end", {}, { turn_number: turn })
    );
  }
  // Whole messages only: a stream that ends mid-turn reconnects instead of going idle.
  return frames;
}

export interface ScaleResult {
  frames: number;
  totalMs: number;
  /** Median cost of one flush (receiving its frames included) over the first and last 100. */
  headBatchMs: number;
  tailBatchMs: number;
  /** Median cost of reading the waiting tool calls after each of the last 100 flushes. */
  tailReadMs: number;
}

/** Replay at least `count` frames and time every flush of BATCH frames. */
export async function measure(count: number): Promise<ScaleResult> {
  const frames = syntheticFrames(count);
  const costs: number[] = [];
  const reads: number[] = [];
  let flush: (() => void) | null = null;
  const state = new PlainSessionState();
  const transport: SessionTransport = {
    async *attach() {
      for (let i = 0; i < frames.length; i += BATCH) {
        const start = performance.now();
        for (const frame of frames.slice(i, i + BATCH)) yield frame;
        const run = flush;
        flush = null;
        run?.();
        const flushed = performance.now();
        waitingCalls(state.agents, () => false);
        costs.push(flushed - start);
        reads.push(performance.now() - flushed);
      }
    },
    submitMessage: () => Promise.reject(new Error("unused")),
    approveTool: async () => {},
    provideCallbackResult: async () => {},
    agentStatus: async (): Promise<AgentStatusSnapshot> => ({ current_turn: 0, turn_active: false, pending_turns: [] }),
    workflowStatus: async (id) => ({ workflow_id: id, execution_status: "RUNNING", closed: false }),
    closeSession: async () => {}
  };
  const core = new AgentSessionCore(
    { sessionId: "s1", transport, idlePollMs: 0, schedule: (f) => (flush = f) },
    state
  );
  const started = performance.now();
  core.start();
  while (state.connection !== "idle") {
    if (state.error) throw state.error;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  const totalMs = performance.now() - started;
  core.stop();
  const median = (xs: number[]) => [...xs].sort((a, b) => a - b)[xs.length >> 1]!;
  return {
    frames: frames.length,
    totalMs,
    headBatchMs: median(costs.slice(0, 100)),
    tailBatchMs: median(costs.slice(-100)),
    tailReadMs: median(reads.slice(-100))
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  for (const count of [5_000, 10_000, 50_000, 100_000]) {
    const r = await measure(count);
    console.log(
      `${String(r.frames).padStart(7)} frames  total ${r.totalMs.toFixed(0).padStart(5)} ms  ` +
        `flush median: first-100 ${r.headBatchMs.toFixed(3)} ms, last-100 ${r.tailBatchMs.toFixed(3)} ms  ` +
        `waitingCalls read, last-100 ${r.tailReadMs.toFixed(3)} ms`
    );
  }
}
