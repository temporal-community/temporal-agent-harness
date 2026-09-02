/**
 * Does a streamed frame actually reach the view?
 *
 * Division of labour with its sibling: check-frame-delivery.mjs owns the commit
 * SCHEDULE — when a batch should land, modelled on a virtual clock against a
 * re-implementation of the policy. It is precise about timing and says nothing
 * about whether anything calls the policy. This file owns the blunter question:
 * boot the real AgentRunController, hand it a real stream, and see whether the
 * frames arrive. Every frame-loss bug this repo has had was of that second kind
 * — the policy was right and nothing invoked it — and a schedule check cannot
 * see them, because a test that re-implements the pipeline always has one.
 *
 * So this loads the shipped module. Vite compiles it for SSR (the runes and the
 * $lib alias come along), the api is injected through the constructor the app
 * already exposes, and the assertions read the same `frames`, `total` and
 * `viewIndex` the components render.
 *
 * What breaks this check. Each of these was applied to the source and the
 * failure it produces observed, so the list is measured rather than hoped for:
 *  - Removing #flushStreamTail: "ends short" reports 0 frames instead of 9,
 *    which is commit 6277f16 reappearing.
 *  - Removing the #armCatchUpFlush call: "stays open" never publishes and the
 *    case times out — the live-session gap, back again.
 *  - Removing the per-frame "is this still the current stream" check in
 *    attach(): "session switch" sees 11 frames instead of 7, the extra four
 *    being the abandoned session's, landed in the session now on screen.
 *  - #schedulePublish no longer being called from #appendFrame, or
 *    #publishFrames no longer copying #frameBuffer into `frames`: every case
 *    reports 0, which is the whole reason this file exists.
 *  - cursorAfterPublish no longer following the live edge: the viewIndex
 *    assertions fail while the counts still pass — a console that receives its
 *    frames and shows you the first one.
 *
 * Run: node ui/scripts/check-frame-arrival.mjs
 */
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Wait for a condition, then return. Polling rather than a fixed sleep so a
 * slow machine does not fail the check, but bounded so a broken pipeline does
 * not hang it.
 */
async function waitFor(label, predicate, timeoutMs = 4_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(25);
  }
  assert.fail(`timed out after ${timeoutMs}ms waiting for ${label}`);
}

// --- browser surface the controller reaches for ------------------------------

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear(),
    key: (i) => [...map.keys()][i] ?? null,
    get length() {
      return map.size;
    }
  };
}

/* Held in a mutable box so each case can be given its own pair.
   The cases below use distinct workflow ids, and the frame cache is keyed by
   session, so sharing one pair would not actually contaminate them today —
   this is insurance, not a live fix. It is worth the three lines because the
   contamination is real when ids do repeat: a scratch harness written while
   building this check shared one store between cases, and a later case
   reported the earlier case's cached frame count while ingesting nothing of
   its own. Per-case storage makes that impossible to reintroduce by reusing an
   id. */
const storage = { local: memoryStorage(), session: memoryStorage() };
const freshStorage = () => {
  storage.local = memoryStorage();
  storage.session = memoryStorage();
};

globalThis.window = {
  get localStorage() {
    return storage.local;
  },
  get sessionStorage() {
    return storage.session;
  },
  setTimeout: (...args) => setTimeout(...args),
  clearTimeout: (...args) => clearTimeout(...args),
  setInterval: (...args) => setInterval(...args),
  clearInterval: (...args) => clearInterval(...args)
};
globalThis.localStorage = new Proxy({}, { get: (_, p) => storage.local[p] });
globalThis.sessionStorage = new Proxy({}, { get: (_, p) => storage.session[p] });
/* A macrotask stands in for a paint: the controller only needs the main thread
   handed back, and nothing here measures frame budget. */
globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

// --- fixtures ----------------------------------------------------------------

const session = (id, over = {}) => ({
  workflow_id: id,
  agent_workflow_type: "IncidentTriageWorkflow",
  run_id: `${id}-run`,
  execution_status: "RUNNING",
  closed: false,
  ...over
});

/**
 * A frame shaped like the wire's. `replay` is what puts the pipeline in
 * catch-up, and `resume_offset` is what a reconnect resumes from, so both have
 * to be real for this to test anything.
 */
const frame = (agentId, offset, { replay = true } = {}) => ({
  event: "reply_delta",
  data: {
    type: "reply_delta",
    agent_id: agentId,
    turn_id: "t1",
    turn_number: 1,
    timestamp: offset,
    resume_offset: offset + 1,
    event_offset: offset,
    delta: `${agentId}#${offset} `,
    replay
  }
});

const abortError = () => Object.assign(new Error("aborted"), { name: "AbortError" });

/**
 * A stream this file drives frame by frame, and can end or drop on cue.
 *
 * `ignoreAbort` models the uncooperative reader: a generator that hands over a
 * frame it had already buffered before noticing it was cancelled. Aborting is
 * therefore not enough on its own, and the controller's own "is this still the
 * current stream" check is the only thing standing between those frames and
 * whichever session is on screen now.
 */
function controllableStream({ ignoreAbort = false } = {}) {
  const queue = [];
  let wake = null;
  let ended = false;
  let failure = null;
  const ping = () => {
    wake?.();
    wake = null;
  };
  return {
    push(...frames) {
      queue.push(...frames);
      ping();
    },
    end() {
      ended = true;
      ping();
    },
    drop(error = new Error("Failed to fetch")) {
      failure = error;
      ping();
    },
    async *iterate(signal) {
      while (true) {
        if (signal?.aborted && !ignoreAbort) throw abortError();
        if (queue.length) {
          yield queue.shift();
          continue;
        }
        if (failure) {
          const error = failure;
          failure = null;
          throw error;
        }
        if (ended) return;
        await new Promise((resolve) => {
          wake = resolve;
          signal?.addEventListener("abort", resolve, { once: true });
        });
      }
    }
  };
}

/**
 * The narrowest api the attach paths touch. Anything the controller calls that
 * is not here throws, which is the point: the check should notice if the
 * pipeline starts depending on something new.
 */
function fakeApi({ streamFor, statusFor }) {
  const attachCalls = [];
  return {
    attachCalls,
    api: {
      async listSessions() {
        return [];
      },
      async agentInterface() {
        return [];
      },
      async operatorInterface() {
        return [];
      },
      async workflowStatus(workflowId) {
        const status = statusFor(workflowId);
        return { workflow_id: workflowId, execution_status: status, closed: status !== "RUNNING" };
      },
      attach(sessionId, fromOffset, signal) {
        attachCalls.push({ sessionId, fromOffset });
        return streamFor(sessionId).iterate(signal);
      }
    }
  };
}

// --- harness -----------------------------------------------------------------

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "silent"
});
const { AgentRunController } = await vite.ssrLoadModule(
  "/src/lib/state/agentRun.svelte.ts"
);

/**
 * A controller with its own storage, its own api, and no other case's state.
 *
 * `finish` reports the workflow as closed from then on, which is how a case says
 * "the run completed" rather than "the connection died". Ending a stream without
 * it leaves the controller correctly retrying for its whole backoff budget.
 */
function boot({ sessions, streamFor, closed = false }) {
  freshStorage();
  const state = { closed };
  const { api, attachCalls } = fakeApi({
    streamFor,
    statusFor: () => (state.closed ? "COMPLETED" : "RUNNING")
  });
  const controller = new AgentRunController(api);
  controller.sessions = sessions;
  return { controller, attachCalls, finish: () => (state.closed = true) };
}

const chunkSize = 24;
const underAChunk = 9;
assert.ok(underAChunk < chunkSize, "the whole point is a backlog too short to fill a chunk");

// --- case: a stream that ends short of a chunk boundary ----------------------
// Commit 6277f16's bug. The chunk gate holds the backlog, the stream ends, and
// without a terminal flush nothing is left to publish it.
{
  const stream = controllableStream();
  const { controller, attachCalls, finish } = boot({
    sessions: [session("wf-ends")],
    streamFor: () => stream
  });

  const selected = controller.selectSession("wf-ends");
  await waitFor("the stream to be attached", () => attachCalls.length === 1);
  stream.push(...Array.from({ length: underAChunk }, (_, i) => frame("root", i)));
  await sleep(120);
  assert.equal(
    controller.frames.length,
    0,
    "a short backlog should still be staged, not published, before the stream ends"
  );

  finish(); // the run completed; this stream ending is not a drop
  stream.end();
  await selected;

  assert.equal(
    controller.frames.length,
    underAChunk,
    "a stream that ends short of a chunk boundary must still publish its backlog"
  );
  assert.equal(controller.total, underAChunk, "total must count the published frames");
  assert.equal(
    controller.viewIndex,
    underAChunk,
    "the scrubber must follow the live edge, not sit at the first frame"
  );
  assert.equal(
    attachCalls.length,
    1,
    "a workflow that has closed must not be re-attached after its stream ends"
  );
}

// --- case: a stream that stays open under a chunk ----------------------------
// The live-session gap. Nothing ends the stream, so no terminal flush can help;
// only the deadline publishes. Waiting has to be sufficient.
{
  const stream = controllableStream();
  const { controller, attachCalls, finish } = boot({
    sessions: [session("wf-open")],
    streamFor: () => stream
  });

  void controller.selectSession("wf-open");
  await waitFor("the stream to be attached", () => attachCalls.length === 1);
  stream.push(...Array.from({ length: underAChunk }, (_, i) => frame("root", i)));

  await sleep(150);
  assert.equal(
    controller.frames.length,
    0,
    "a short backlog must batch first, or the batching is not doing its job"
  );

  await waitFor(
    "the deadline to publish a still-open stream",
    () => controller.frames.length === underAChunk
  );
  assert.equal(controller.total, underAChunk, "total must count the deadline-published frames");
  assert.equal(
    controller.viewIndex,
    underAChunk,
    "the scrubber must follow the live edge after a deadline commit too"
  );
  assert.equal(
    attachCalls.length,
    1,
    "a stream that is still open must not be re-attached underneath itself"
  );
  finish();
  stream.end();
}

// --- case: a session switch mid-stream ---------------------------------------
// The buffer staged against the old session must never land in the new one. If
// the guards weaken, this is where it shows: as another session's frames.
{
  const streams = {
    "wf-old": controllableStream({ ignoreAbort: true }),
    "wf-new": controllableStream()
  };
  const { controller, attachCalls, finish } = boot({
    sessions: [session("wf-old"), session("wf-new")],
    streamFor: (id) => streams[id]
  });

  void controller.selectSession("wf-old");
  await waitFor("the old stream to be attached", () => attachCalls.length === 1);
  streams["wf-old"].push(...Array.from({ length: 5 }, (_, i) => frame("old", i)));
  await sleep(120);
  assert.equal(controller.frames.length, 0, "the old session's backlog should still be staged");

  // Switch while that backlog is staged and its stream is still open.
  const selected = controller.selectSession("wf-new");
  streams["wf-old"].push(...Array.from({ length: 4 }, (_, i) => frame("old", 100 + i)));
  await waitFor(
    "the new stream to be attached",
    () => attachCalls.some((call) => call.sessionId === "wf-new")
  );
  streams["wf-new"].push(...Array.from({ length: 7 }, (_, i) => frame("new", i)));
  finish();
  streams["wf-new"].end();
  await selected;

  assert.equal(
    controller.frames.length,
    7,
    "the new session must show its own frames and only its own"
  );
  assert.deepEqual(
    [...new Set(controller.frames.map((item) => item.data.agent_id))],
    ["new"],
    "a buffer staged against the previous session must never land in the new one"
  );
  assert.equal(controller.viewIndex, 7, "the scrubber must follow the new session's live edge");
  streams["wf-old"].end();
}

await vite.close();
console.log("frame arrival: ok");
process.exit(0);
