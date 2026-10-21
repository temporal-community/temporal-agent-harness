import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, test } from "node:test";
import { AgentClient } from "../src/harness/agent-client.ts";
import { connectClient } from "../src/web/client.ts";
import { runAgentWorker } from "../src/index.ts";
import { FIXTURES, startServer, type TemporalServer } from "./support.ts";

let server: TemporalServer;
before(async () => {
  server = await startServer();
  // runAgentWorker and connectClient read the same TEMPORAL_* env as the CLI.
  process.env.TEMPORAL_ADDRESS = server.address;
  process.env.TEMPORAL_NAMESPACE = server.namespace;
});
after(async () => {
  await server.teardown();
});

test("runAgentWorker serves the agents module until its signal aborts", async () => {
  const id = randomUUID().slice(0, 8);
  const taskQueue = `run-agent-worker-${id}`;
  const sessionId = `echo-session-${id}`;
  const stop = new AbortController();
  const running = runAgentWorker({ agents: FIXTURES, taskQueue, signal: stop.signal });
  const client = await connectClient();
  try {
    const agent = new AgentClient(client, sessionId);
    await agent.startAndSubmitMessage(
      "ask",
      { text: "hi" },
      { workflowType: "InteropEcho", taskQueue, startConfig: {} },
    );
    let turn = 0;
    for (let i = 0; i < 100 && turn < 1; i++) {
      turn = await agent.getStatus().then(
        (s) => (s.turn_active ? 0 : s.current_turn),
        () => 0,
      );
      if (turn < 1) await new Promise((r) => setTimeout(r, 200));
    }
    assert.equal(turn, 1);
  } finally {
    await client.workflow
      .getHandle(sessionId)
      .terminate("test finished")
      .catch(() => {});
    stop.abort();
    await running;
    await client.connection.close();
  }
});
