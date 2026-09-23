// useAgentSession under React: the connection follows whether a component using the session is
// mounted, sessions are shared through a provider, and renders see the stream as it arrives.

import { act, cleanup, render, screen } from "@testing-library/react";
import { StrictMode, useState } from "react";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, test } from "vitest";
import type {
  AgentSseFrame,
  AgentStatusSnapshot,
  SessionTransport,
  SubmitMessageResponse,
  WorkflowExecutionState
} from "@temporal-agent-harness/client";

import { HarnessProvider, useAgentSession, type AgentSession } from "../src/index.js";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(cleanup);

type Step = AgentSseFrame | { hold: Promise<void> };

/** Plays one script per attach, per session id, then ends the attach. A `hold` step keeps the
 *  attach open until it resolves or the attach is aborted. */
class FakeTransport implements SessionTransport {
  readonly attaches: string[] = [];
  readonly scripts = new Map<string, Step[][]>();
  open = 0;

  async *attach(sessionId: string, _from: number, signal: AbortSignal): AsyncIterable<AgentSseFrame> {
    this.attaches.push(sessionId);
    this.open += 1;
    try {
      for (const step of this.scripts.get(sessionId)?.shift() ?? []) {
        if (signal.aborted) return;
        await Promise.resolve();
        if ("hold" in step) {
          await Promise.race([step.hold, new Promise((resolve) => signal.addEventListener("abort", resolve))]);
          continue;
        }
        yield step;
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

function hold() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => (release = resolve));
  return { step: { hold: promise }, release };
}

/** Wait, letting React commit, until `check` holds. */
async function until(check: () => boolean, ms = 2_000): Promise<void> {
  const deadline = Date.now() + ms;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out waiting for a condition");
    await act(() => new Promise((resolve) => setTimeout(resolve, 1)));
  }
}

/** Release is deferred a microtask, so a StrictMode remount does not reconnect. */
const released = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)));

const quick = { idlePollMs: 0, schedule: (flush: () => void) => queueMicrotask(flush) };

/** Renders the root agent's messages as `id:status`, and hands the session to `onSession`. */
function Messages(props: { sessionId: string; transport: SessionTransport; onSession?: (s: AgentSession) => void }) {
  const session = useAgentSession({ sessionId: props.sessionId, transport: props.transport, ...quick });
  props.onSession?.(session);
  return <p data-testid={`messages-${props.sessionId}`}>{session.messages.map((m) => `${m.id}:${m.status}`).join(",")}</p>;
}

describe("useAgentSession", () => {
  test("mounting connects it, messages change once per flush, and unmounting disconnects it", async () => {
    const transport = new FakeTransport();
    transport.scripts.set("s1", [exchange("m1", "hello")]);
    const lists = new Set<readonly unknown[]>();
    const connections: string[] = [];
    let latest: AgentSession | undefined;
    function Probe() {
      // Flushed only when the stream ends, so the whole exchange lands as one write.
      latest = useAgentSession({ sessionId: "s1", transport, idlePollMs: 0, schedule: () => {} });
      lists.add(latest.messages);
      connections.push(latest.connection);
      return null;
    }

    const { unmount } = render(<Probe />);
    /* Nothing connects during render: only once the component has mounted. */
    expect(connections[0]).toBe("stopped");
    await until(() => latest?.connection === "idle");
    expect(transport.attaches).toEqual(["s1"]);
    /* The empty list, then the finished exchange: never a list mid-exchange. */
    expect([...lists].map((l) => l.length)).toEqual([0, 1]);
    expect(latest!.messages[0]!.status).toBe("done");

    unmount();
    await released();
    expect(transport.open).toBe(0);
  });

  test("components following one session under a provider share one connection", async () => {
    const transport = new FakeTransport();
    const { step, release } = hold();
    transport.scripts.set("s1", [[...exchange("m1", "hi"), step]]);
    let a: AgentSession | undefined;
    let b: AgentSession | undefined;

    function Both(props: { showA: boolean }) {
      return (
        <HarnessProvider>
          {props.showA && <Messages sessionId="s1" transport={transport} onSession={(s) => (a = s)} />}
          <Messages sessionId="s1" transport={transport} onSession={(s) => (b = s)} />
        </HarnessProvider>
      );
    }
    const { rerender, unmount } = render(<Both showA />);
    await until(() => b?.messages.length === 1);
    expect(transport.attaches).toEqual(["s1"]);
    expect(a!.messages).toBe(b!.messages);

    rerender(<Both showA={false} />);
    await released();
    expect(transport.open).toBe(1);

    unmount();
    await released();
    expect(transport.open).toBe(0);
    release();
  });

  test("without a provider, each component has a connection of its own", async () => {
    const transport = new FakeTransport();
    transport.scripts.set("s1", [exchange("m1", "one"), exchange("m1", "one")]);
    render(
      <>
        <Messages sessionId="s1" transport={transport} />
        <Messages sessionId="s1" transport={transport} />
      </>
    );
    await until(() => transport.attaches.length === 2);
  });

  test("a changed sessionId moves the connection", async () => {
    const transport = new FakeTransport();
    transport.scripts.set("s1", [exchange("m1", "first")]);
    transport.scripts.set("s2", [exchange("m2", "second")]);
    let pick: (id: string) => void = () => {};
    function Switcher() {
      const [id, setId] = useState("s1");
      pick = setId;
      return <Messages sessionId={id} transport={transport} />;
    }

    render(<Switcher />);
    await until(() => screen.queryByTestId("messages-s1")?.textContent === "m1:done");
    act(() => pick("s2"));
    await until(() => screen.queryByTestId("messages-s2")?.textContent === "m2:done");
    expect(transport.attaches).toEqual(["s1", "s2"]);
  });

  test("StrictMode's mount, unmount and remount opens one connection", async () => {
    const transport = new FakeTransport();
    const { step, release } = hold();
    transport.scripts.set("s1", [[...exchange("m1", "hi"), step]]);
    const { unmount } = render(
      <StrictMode>
        <Messages sessionId="s1" transport={transport} />
      </StrictMode>
    );
    await until(() => screen.getByTestId("messages-s1").textContent === "m1:done");
    await released();
    expect(transport.attaches).toEqual(["s1"]);
    expect(transport.open).toBe(1);
    unmount();
    await released();
    expect(transport.open).toBe(0);
    release();
  });

  test("waiting approvals come from the whole tree, and pendingCallbacks leaves out callbackTools", async () => {
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
        frame("tool_approval_requested", { tool_id: "gate", tool_name: "rm", tool_input: { path: "/" } }, child),
        frame("callback_requested", { tool_id: "c1", tool_name: "read_file", tool_input: {}, output_schema: {} }, root),
        frame("callback_requested", { tool_id: "c2", tool_name: "confirm_purchase", tool_input: {}, output_schema: {} }, root)
      ]
    ]);
    let session: AgentSession | undefined;
    function Waiting() {
      session = useAgentSession({ sessionId: "s1", transport, callbackTools: { read_file: () => "contents" }, ...quick });
      return null;
    }

    render(<Waiting />);
    await until(() => session?.pendingApprovals.length === 1 && session.messages[0]?.parts.length === 3);
    const [waiting] = session!.pendingApprovals;
    expect([waiting!.agentId, waiting!.messageId, waiting!.part.toolName]).toEqual(["root.s1", "c1", "rm"]);
    expect(session!.pendingCallbacks.map((w) => w.part.toolName)).toEqual(["confirm_purchase"]);
    expect(session!.agent("root.s1")?.workflowId).toBe("wf-s1");
  });

  test("rendered on the server, it shows the empty session and opens no connection", () => {
    const transport = new FakeTransport();
    transport.scripts.set("s1", [exchange("m1", "hi")]);
    function Status() {
      const session = useAgentSession({ sessionId: "s1", transport, ...quick });
      return <p>{`${session.connection}:${session.messages.length}`}</p>;
    }
    expect(renderToString(<Status />)).toBe("<p>stopped:0</p>");
    expect(transport.attaches).toEqual([]);
  });
});
