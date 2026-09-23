// AgentSession under the Svelte client runtime: the connection follows whether anything reads
// the session, sessions are shared through a store, and effects see the stream as it arrives.

import { flushSync } from "svelte";
import { describe, expect, test } from "vitest";
import type {
  AgentSseFrame,
  AgentStatusSnapshot,
  SessionTransport,
  SubmitMessageResponse,
  WorkflowExecutionState
} from "@temporal-agent-harness/client";

import { AgentSession, SessionStore } from "../src/lib/index.js";

/** Plays one script of frames per attach, per session id; then ends each attach. */
class FakeTransport implements SessionTransport {
  readonly attaches: string[] = [];
  readonly scripts = new Map<string, AgentSseFrame[][]>();
  open = 0;

  async *attach(sessionId: string, _from: number, signal: AbortSignal): AsyncIterable<AgentSseFrame> {
    this.attaches.push(sessionId);
    this.open += 1;
    try {
      for (const frame of this.scripts.get(sessionId)?.shift() ?? []) {
        if (signal.aborted) return;
        await Promise.resolve();
        yield frame;
      }
    } finally {
      this.open -= 1;
    }
  }
  async submitMessage(): Promise<SubmitMessageResponse> {
    return { turn_number: 1, turn_id: "t", message_id: "m-sent", accepted_offset: 0, disposition: "opened" };
  }
  async approveTool(): Promise<void> {}
  async provideCallbackResult(): Promise<void> {}
  async agentStatus(): Promise<AgentStatusSnapshot> {
    return { current_turn: 0, turn_active: false, pending_turns: [] };
  }
  async workflowStatus(sessionId: string): Promise<WorkflowExecutionState> {
    return { workflow_id: sessionId, execution_status: "RUNNING", closed: false };
  }
  async closeSession(): Promise<void> {}
}

let offset = 0;
function frame(event: string, data: Record<string, unknown>, env: { agent_id?: string; message_id?: string | null } = {}): AgentSseFrame {
  return {
    event,
    data: {
      type: event,
      agent_id: env.agent_id ?? "root",
      turn_id: "t1",
      turn_number: 1,
      message_id: env.message_id ?? null,
      timestamp: ++offset,
      resume_offset: offset,
      ...data
    }
  } as unknown as AgentSseFrame;
}

function exchange(id: string, text: string): AgentSseFrame[] {
  const env = { message_id: id };
  return [
    frame("message_accepted", { handler: "ask", payload: { text }, disposition: "opened" }, env),
    frame("turn_started", {}),
    frame("reply_delta", { text }, env),
    frame("message_handler_end", { output: { text } }, env),
    frame("turn_end", {})
  ];
}

async function until(check: () => boolean, ms = 2_000): Promise<void> {
  const deadline = Date.now() + ms;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out waiting for a condition");
    await new Promise((resolve) => setTimeout(resolve, 1));
    flushSync();
  }
}

/** Svelte releases a subscription in a microtask after its last reader goes. */
const released = () => new Promise((resolve) => setTimeout(resolve, 0));

/** Read `read` in an effect, as a component would; returns the teardown. */
function watch(read: () => unknown): () => void {
  return $effect.root(() => {
    $effect(() => {
      read();
    });
  });
}

const quick = { idlePollMs: 0, schedule: (flush: () => void) => queueMicrotask(flush) };

describe("AgentSession", () => {
  test("reading it in an effect connects it, and the last reader leaving disconnects it", async () => {
    const transport = new FakeTransport();
    transport.scripts.set("s1", [exchange("m1", "hello")]);
    // Flushed only when the stream ends, so the whole exchange lands as one write.
    const session = new AgentSession({ sessionId: "s1", transport, idlePollMs: 0, schedule: () => {} });

    expect(session.connection).toBe("stopped");
    expect(transport.attaches).toEqual([]);

    const seen: string[] = [];
    const stop = $effect.root(() => {
      $effect(() => {
        seen.push(session.messages.map((m) => `${m.id}:${m.status}`).join(","));
      });
    });
    flushSync();
    await until(() => session.connection === "idle");

    expect(transport.attaches).toEqual(["s1"]);
    // One re-run for the flush, not one per frame.
    expect(seen).toEqual(["", "m1:done"]);

    stop();
    await released();
    expect(session.connection).toBe("stopped");
  });

  test("sessions for one id in a shared store share one connection", async () => {
    const transport = new FakeTransport();
    transport.scripts.set("s1", [exchange("m1", "hi")]);
    const store = new SessionStore();
    const a = new AgentSession({ sessionId: "s1", transport, ...quick }, store);
    const b = new AgentSession({ sessionId: "s1", transport, ...quick }, store);

    const stopA = watch(() => a.messages);
    const stopB = watch(() => b.messages);
    flushSync();
    await until(() => b.messages.length === 1);

    expect(transport.attaches).toEqual(["s1"]);
    expect(a.messages).toBe(b.messages);

    stopA();
    await released();
    expect(b.connection).not.toBe("stopped");
    stopB();
    await released();
    expect(b.connection).toBe("stopped");
  });

  test("a getter sessionId moves the connection when it changes", async () => {
    const transport = new FakeTransport();
    transport.scripts.set("s1", [exchange("m1", "first")]);
    transport.scripts.set("s2", [exchange("m2", "second")]);
    let current = $state("s1");
    const session = new AgentSession({
      get sessionId() {
        return current;
      },
      transport,
      ...quick
    });

    const stop = watch(() => session.messages);
    flushSync();
    await until(() => session.messages[0]?.id === "m1");

    current = "s2";
    flushSync();
    await until(() => session.messages[0]?.id === "m2");
    expect(transport.attaches).toEqual(["s1", "s2"]);
    stop();
  });

  test("subagents and waiting approvals anywhere in the tree are exposed", async () => {
    const transport = new FakeTransport();
    const root = { message_id: "a" };
    const child = { agent_id: "root.s1", message_id: "c1" };
    transport.scripts.set("s1", [
      [
        frame("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, root),
        frame("subagent_message_sent", {
          subagent_id: "root.s1", agent_key: "helper", workflow_id: "wf-s1", handler: "ask", subagent_turn: 1, from_offset: 0
        }, root),
        frame("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, child),
        frame("tool_approval_requested", { tool_id: "gate", tool_name: "rm", tool_input: { path: "/" } }, child)
      ]
    ]);
    const session = new AgentSession({ sessionId: "s1", transport, ...quick });
    const stop = watch(() => session.pendingApprovals);
    flushSync();
    await until(() => session.pendingApprovals.length === 1);

    const [waiting] = session.pendingApprovals;
    expect([waiting!.agentId, waiting!.messageId, waiting!.part.toolName]).toEqual(["root.s1", "c1", "rm"]);
    expect(session.agent("root.s1")?.workflowId).toBe("wf-s1");
    const link = session.messages[0]!.parts[0];
    expect(link?.type === "subagent" ? link.messageId : null).toBe("c1");
    stop();
  });

  test("pendingCallbacks leaves out the tools callbackTools answers", async () => {
    const transport = new FakeTransport();
    const env = { message_id: "a" };
    transport.scripts.set("s1", [
      [
        frame("message_accepted", { handler: "ask", payload: {}, disposition: "opened" }, env),
        frame("callback_requested", { tool_id: "c1", tool_name: "read_file", tool_input: {}, output_schema: {} }, env),
        frame("callback_requested", { tool_id: "c2", tool_name: "confirm_purchase", tool_input: {}, output_schema: {} }, env)
      ]
    ]);
    /* The call to `read_file` stays waiting until the stream reports it resolved, which this
       script never does, so it is left out for having a handler, not for being done. */
    const session = new AgentSession({
      sessionId: "s1",
      transport,
      callbackTools: { read_file: () => "contents" },
      ...quick
    });
    const stop = watch(() => session.pendingCallbacks);
    flushSync();
    await until(() => session.messages[0]?.parts.length === 2);

    expect(session.pendingCallbacks.map((w) => w.part.toolName)).toEqual(["confirm_purchase"]);
    stop();
  });

  test("an unread session opens no connection, even when sent to", async () => {
    const transport = new FakeTransport();
    const session = new AgentSession({ sessionId: "s1", transport, ...quick });
    await session.sendMessage("ask", { text: "hi" });
    expect(transport.attaches).toEqual([]);
  });
});
