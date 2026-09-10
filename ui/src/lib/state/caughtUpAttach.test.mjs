/**
 * A caught-up session must not look like a broken one.
 *
 * /api/attach ends its response as soon as it has nothing further to send, so a
 * RUNNING session the reader has already caught up with answers `200` with an
 * empty body in about ten milliseconds — measured against the dev stack, three
 * times, at the offset the previous attach ended on. That is the steady state of
 * every live session anyone leaves open, not an error.
 *
 * The reconnect cannot tell it apart from a dropped stream: the generator ends
 * the same way and the workflow is still RUNNING, so #streamDroppedMidRun says
 * "keep trying" and the loop spends its whole budget — seven attaches and seven
 * status probes over 31.5s of backoff. selectSession() awaits that loop, so
 * `connecting` stayed true the entire time and the console showed "connecting"
 * on a session whose transcript was already fully loaded. Measured at 31,513ms
 * against this controller; the endpoint people blamed answers in 104ms.
 *
 * What breaks this test. Each was applied to the source and the failure
 * observed, so the list is measured rather than hoped for:
 *  - Holding `connecting` across the retry loop: "caught up" times out waiting
 *    for it to clear, which is the 31.5s stall back again.
 *  - Not retrying a stream that dropped after delivering: "real drop" fails,
 *    which is a live run whose transport died mid-flight and is never
 *    re-attached, so the console stops updating on a workflow still running.
 *  - Letting an in-band `error` frame count as delivery: "error frame" sees the
 *    backoff stall at 500ms instead of walking 500/1000/2000, which on a real
 *    server is an unbounded re-attach loop.
 *  - Clearing `sending` only in attach()'s `finally`: "whole budget" sees the
 *    composer held for 31.5s after the reply already arrived.
 *  - Clearing `creatingSession` only in startNewSession()'s `finally`: "brand-new
 *    session" sees the composer held for the whole budget, measured at 34,071ms
 *    in a browser, which is a new session that can never be sent anything.
 *  - Making an exhausted budget raise on a clean end: "whole budget" sees a
 *    banner on a session that is fine.
 *  - Guarding the `finally` flush on the stream version: "closes mid-backlog"
 *    sees an empty transcript where five staged frames should be, because the
 *    close being handled is what bumped the version.
 *
 * What does NOT break it, checked rather than assumed: dropping the
 * `isCurrentStream()` guard on the clear. The loop re-tests it at the top and
 * nothing awaits between the stream ending and the clear, so an abandoned loop
 * cannot reach that line — "session switch" passes either way. The guard is
 * kept as a guard-rail, matching the session-keyed one on the flush above it,
 * and that case pins the behaviour rather than the guard producing it.
 */
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { installBrowserSurface, sleep } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";
import { catchUpCeilingMs } from "./hydration.ts";

async function waitFor(label, predicate, timeoutMs = 6_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(25);
  }
  assert.fail(`timed out after ${timeoutMs}ms waiting for ${label}`);
}

const { freshStorage } = installBrowserSurface();

const session = (id) => ({
  workflow_id: id,
  agent_workflow_type: "IncidentCommander",
  run_id: `${id}-run`,
  execution_status: "RUNNING",
  closed: false
});

const frame = (offset) => ({
  event: "reply_delta",
  data: {
    type: "reply_delta",
    agent_id: "root",
    turn_id: "t1",
    turn_number: 1,
    timestamp: offset,
    resume_offset: offset + 1,
    event_offset: offset,
    delta: `#${offset} `,
    replay: true
  }
});

function boot({ sessions, streamFor, statusFor, listSessionsFor = async () => [] }) {
  freshStorage();
  const attachCalls = [];
  const statusCalls = [];
  const api = {
    async listAgents() {
      return [{ key: "qa", label: "QA", workflow_type: "X", task_queue: "q", description: "" }];
    },
    async submitMessage() {
      return { ok: true };
    },
    async listSessions() {
      return listSessionsFor();
    },
    async createSession() {
      return session("wf-created");
    },
    async agentInterface() {
      return [];
    },
    async operatorInterface() {
      return [];
    },
    async workflowStatus(workflowId) {
      statusCalls.push(workflowId);
      const status = statusFor(workflowId);
      return {
        workflow_id: workflowId,
        execution_status: status,
        closed: status !== "RUNNING"
      };
    },
    attach(sessionId, fromOffset, signal, onConnected) {
      attachCalls.push({ sessionId, fromOffset, at: Date.now() });
      return streamFor(sessionId, attachCalls.length, signal, onConnected);
    }
  };
  const controller = new AgentRunController(api);
  controller.sessions = sessions;
  return { controller, attachCalls, statusCalls };
}

/**
 * The shape /api/attach reports a failure with: `kind`/`code` and no `type`,
 * carrying a fact about the connection rather than about the run. See
 * _attach_error in web/app.py.
 */
const errorFrame = (code) => ({
  event: "error",
  data: { kind: "unavailable", code, message: `synthetic ${code}` }
});

/** A stream that ends immediately having said nothing, the way a caught-up attach does. */
// eslint-disable-next-line require-yield
async function* silent() {
  return;
}

describe("a caught-up attach", () => {
  it("marks a quiet resumed stream connected when HTTP headers arrive", async () => {
    let finish;
    const heldOpen = new Promise((resolve) => { finish = resolve; });
    const { controller, attachCalls } = boot({
      sessions: [session("wf-quiet-live")],
      streamFor: (_id, _call, _signal, onConnected) => (async function* () {
        onConnected();
        await heldOpen;
      })(),
      statusFor: () => "RUNNING"
    });

    void controller.selectSession("wf-quiet-live");
    try {
      await waitFor("a connected stream without waiting for events", () => attachCalls.length > 0 && !controller.connecting);
      assert.equal(controller.frames.length, 0, "connection does not invent an agent action");
    } finally {
      finish();
    }
  });

  it("marks a long-running attach connected as soon as a real frame arrives", async () => {
    let finish;
    const heldOpen = new Promise((resolve) => { finish = resolve; });
    const { controller } = boot({
      sessions: [session("wf-live-tool")],
      streamFor: () => (async function* () {
        yield frame(0);
        await heldOpen;
      })(),
      statusFor: () => "RUNNING"
    });

    void controller.selectSession("wf-live-tool");
    try {
      await waitFor("a connected live stream without waiting for EOF", () => !controller.connecting);
      assert.equal(controller.connectionError, null);
    } finally {
      finish();
    }
  });

  it("does not treat an in-band connection error as a successful connection", async () => {
    let finish;
    let deliveredError = false;
    const heldOpen = new Promise((resolve) => { finish = resolve; });
    const { controller } = boot({
      sessions: [session("wf-error-open")],
      streamFor: () => (async function* () {
        yield errorFrame("stream_unavailable");
        deliveredError = true;
        await heldOpen;
      })(),
      statusFor: () => "RUNNING"
    });

    void controller.selectSession("wf-error-open");
    try {
      await waitFor("the in-band connection error", () => deliveredError);
      assert.equal(controller.connecting, true);
      assert.match(controller.connectionError, /stream_unavailable/);
    } finally {
      finish();
    }
  });

  // Every attach answers empty and ends, which is what the dev server does at an
  // offset the reader already holds. The retries that follow are background
  // liveness and must not present as loading.
  it("caught up on a RUNNING session", async () => {
    const { controller, attachCalls, statusCalls } = boot({
      sessions: [session("wf-caught-up")],
      streamFor: () => silent(),
      statusFor: () => "RUNNING"
    });

    const started = Date.now();
    void controller.selectSession("wf-caught-up");
    await waitFor("the first attach", () => attachCalls.length === 1);
    await waitFor("connecting to clear", () => !controller.connecting, 3_000);
    const elapsed = Date.now() - started;

    assert.ok(
      elapsed < 2_000,
      `a caught-up session must present as loaded promptly (took ${elapsed}ms)`
    );

    /* The re-attach itself is left alone on purpose: it is the only thing that
       carries a new event to a reader who is already caught up, because
       /api/attach does not hold the connection open. Making it unnecessary is a
       server-side fix, so what is asserted here is only that it stays silent. */
    await sleep(1_200);
    assert.equal(
      controller.connecting,
      false,
      "background re-attaches must never put the console back into 'connecting'"
    );
    assert.ok(statusCalls.length >= 1, "a stream that ended is still worth a status probe");
  });

  // The clear lives inside the retry loop, so an abandoned loop is still running
  // while the next session connects. Whatever provides it, the flag the new
  // session set has to survive.
  it("a session switched away from mid-retry", async () => {
    const { controller, attachCalls } = boot({
      sessions: [session("wf-left"), session("wf-joined")],
      streamFor: (id) => (id === "wf-left" ? silent() : (async function* () {
        await sleep(50_000);
      })()),
      statusFor: () => "RUNNING"
    });

    void controller.selectSession("wf-left");
    await waitFor("the abandoned session to attach", () => attachCalls.length === 1);

    void controller.selectSession("wf-joined");
    await waitFor(
      "the new session to attach",
      () => attachCalls.some((call) => call.sessionId === "wf-joined")
    );
    /* Long enough for the abandoned loop's first backoff (500ms) to elapse, which
       is when an unguarded clear would fire against the wrong session. */
    await sleep(900);
    assert.equal(
      controller.connecting,
      true,
      "a retry loop left behind must not clear the flag the current session set"
    );
  });

  // The other half of the pair above. Frames arrived, then the transport failed,
  // and the workflow is still RUNNING: that is a drop, not a caught-up session,
  // and it must resume from the offset the server already proved it holds.
  it("a stream that really did drop must still be retried", async () => {
    const { controller, attachCalls } = boot({
      sessions: [session("wf-drop")],
      streamFor: (_id, call) =>
        (async function* () {
          if (call === 1) {
            for (let i = 0; i < 3; i += 1) yield frame(i);
            throw new Error("Failed to fetch");
          }
          await sleep(50_000);
        })(),
      statusFor: () => "RUNNING"
    });

    void controller.selectSession("wf-drop");
    await waitFor("a re-attach after a genuine drop", () => attachCalls.length === 2, 6_000);
    assert.equal(
      attachCalls[1].fromOffset,
      3,
      "a reconnect must resume from the last offset the server sent"
    );
    assert.equal(controller.frames.length, 3, "the delivered frames must reach the view");
  });

  // `stream_unavailable` is worth retrying, so #streamDroppedMidRun keeps saying
  // yes and only the budget can end it. Counting the frame as delivery resets
  // `attempt` every pass, so the budget never ages and the loop never stops.
  it("an in-band error frame must not renew the retry budget", async () => {
    const { controller, attachCalls } = boot({
      sessions: [session("wf-error")],
      streamFor: () =>
        (async function* () {
          yield errorFrame("stream_unavailable");
        })(),
      statusFor: () => "RUNNING"
    });

    void controller.selectSession("wf-error");
    await waitFor("three attaches, enough to see the interval move", () => attachCalls.length >= 3);

    /* The gaps walk 500 -> 1000 -> 2000 iff the frame was not counted. When it is
       counted every gap stays 500ms forever, so the second gap is the whole test
       — and it needs no 31.5s wait to see. */
    const gaps = attachCalls.slice(1).map((call, i) => call.at - attachCalls[i].at);
    assert.ok(
      gaps[1] >= 900,
      `an error frame must not renew the retry budget — backoff stalled at ${gaps.join("/")}ms`
    );
  });

  // The slow one, and the only case that reaches the end of the backoff. Two
  // things are only observable there.
  //
  // `sending` gates the composer, and it was cleared in attach()'s `finally` —
  // after every retry. So a reply that had fully arrived left the input locked
  // for the whole budget: measured at 31,536ms, against a run that finished in
  // under a second.
  //
  // And a budget that runs out must stay silent. It does today, because a
  // caught-up stream ends cleanly and only the `catch` branch rethrows, so
  // exhaustion is a `break`. That is worth pinning rather than trusting: a false
  // banner on a healthy session is worse than the stall it replaced, because it
  // teaches people to disbelieve real ones.
  it("the whole budget, on a session that is perfectly healthy", async () => {
    const { controller, attachCalls } = boot({
      sessions: [session("wf-budget")],
      streamFor: (_id, call) =>
        (async function* () {
          // The reply lands on the first attach; every later one is caught up.
          if (call === 1) for (let i = 0; i < 4; i += 1) yield frame(i);
        })(),
      statusFor: () => "RUNNING"
    });

    await controller.initialize();
    controller.sessions = [session("wf-budget")];
    controller.session = controller.sessions[0];
    attachCalls.length = 0;

    let firstError = null;
    const watch = setInterval(() => {
      if (controller.connectionError && !firstError) firstError = controller.connectionError;
    }, 20);

    const started = Date.now();
    void controller.sendMessage({ text: "hello" });
    await waitFor("the reply to arrive", () => controller.frames.length === 4);
    await waitFor("the composer to be released", () => !controller.sending, 2_000);
    const releasedAt = Date.now() - started;
    assert.ok(
      releasedAt < 2_000,
      `a reply that has arrived must release the composer (took ${releasedAt}ms)`
    );

    /* Out past the last backoff step, so the loop has given up and anything it
       says on the way out has been said. */
    await waitFor(
      "the retry budget to run out",
      () => Date.now() - started > 34_000,
      40_000
    );
    clearInterval(watch);

    assert.equal(firstError, null, `a healthy session must never raise a banner (saw ${firstError})`);
    assert.equal(controller.connectionError, null, "and must not be left showing one");
    assert.equal(controller.sending, false, "the composer must still be released");

    const settled = attachCalls.length;
    await sleep(1_000);
    assert.equal(attachCalls.length, settled, "the budget must actually stop the loop");
  });

  // The worst instance of the same defect, and the only one that hits every time.
  // A session created a moment ago has published nothing, so its first attach —
  // and every retry after it — answers empty while the workflow stays RUNNING:
  // the budget always drains in full. `creatingSession` gates the composer
  // outright, so the one thing a new session exists for was locked out for the
  // whole 34s, nothing could be sent, and nothing streamed. Reloading appeared to
  // fix it only because initialize() reaches the same attach without ever setting
  // the flag.
  it("a brand-new session must not lock its own composer", async () => {
    const { controller, attachCalls } = boot({
      sessions: [],
      streamFor: () => silent(),
      statusFor: () => "RUNNING"
    });

    /* Pre-seeded so #loadAgents short-circuits: the other cases lean on
       listAgents() failing to keep initialize() cheap, and this case only needs
       the create path. */
    controller.agents = [
      { key: "qa", label: "QA", workflow_type: "X", task_queue: "q", description: "" }
    ];

    const started = Date.now();
    void controller.startNewSession("X");
    await waitFor("the new session to attach", () => attachCalls.length === 1);
    await waitFor("the composer to be released", () => !controller.creatingSession, 4_000);
    const releasedAt = Date.now() - started;

    assert.ok(
      releasedAt < 2_000,
      `a new session must accept its first message promptly (composer held ${releasedAt}ms)`
    );
    assert.equal(controller.connecting, false, "and must not still present as loading");
  });

  // Replay frames put the pipeline in catch-up, where commits happen every 24
  // frames or once the 1s ceiling expires — so a partial chunk is staged, not on
  // screen. Learning the workflow closed calls #stopStream, which aborts the read;
  // the abort breaks out of the retry loop above the flush that exists for exactly
  // this, and lands in the `finally`. Guarding that flush on the stream version
  // declines it, because the close being handled is what bumped the version.
  it("a run that closes mid-backlog must not sit on its staged tail", async () => {
    const backlog = [frame(0), frame(1), frame(2), frame(3), frame(4)];
    let delivered;
    const allYielded = new Promise((resolve) => {
      delivered = resolve;
    });

    const { controller } = boot({
      sessions: [session("wf-closing")],
      async *streamFor(_id, _attempt, signal) {
        for (const item of backlog) yield item;
        delivered();
        /* Hold the stream open the way a live attach does, and end it only the way
           the close does — by aborting. Returning here instead would leave the loop
           to reach the in-loop flush, which is the path that already works. */
        await new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError"))
          );
        });
      },
      statusFor: () => "RUNNING",
      listSessionsFor: async () => [
        { ...session("wf-closing"), execution_status: "COMPLETED", closed: true }
      ]
    });

    void controller.selectSession("wf-closing");
    await allYielded;

    assert.equal(
      controller.frames.length,
      0,
      "fixture: a partial chunk must still be staged when the run closes, or this case proves nothing"
    );

    await controller.refreshSessions();
    await sleep(50);

    assert.equal(
      controller.frames.length,
      backlog.length,
      `a run that closes mid-backlog must publish its tail at once, not hold it for the ${catchUpCeilingMs}ms ceiling`
    );
    assert.equal(
      controller.session?.closed,
      true,
      "and the close itself must still land"
    );
  });
});
