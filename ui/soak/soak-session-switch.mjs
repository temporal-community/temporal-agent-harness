/**
 * Session-switch isolation under load — abandoned attach cannot leak frames.
 *
 * Complements the frame-arrival "session switch" case with a rapid A→B→A loop
 * and explicit teardown bounds (no unbounded frames growth across switches).
 *
 * Run: node ui/soak/soak-session-switch.mjs
 */
import assert from "node:assert/strict";
import {
  installBrowserSurface,
  loadControllerModules,
  session,
  sleep,
  variedFrame,
  waitFor
} from "./lib/controllerHarness.mjs";
import { controllableStream, createMockApi } from "./lib/mockApi.mjs";

const SWITCHES = 20;
const BURST = 40;

const { freshStorage } = installBrowserSurface();
const { vite, AgentRunController } = await loadControllerModules(import.meta.url);

const streams = {
  "wf-a": controllableStream({ ignoreAbort: true }),
  "wf-b": controllableStream({ ignoreAbort: true })
};

freshStorage();
const { api, attachCalls, counts } = createMockApi({
  sessions: () => [session("wf-a"), session("wf-b")],
  streamFor: (id) => streams[id]
});

const controller = new AgentRunController(api);
controller.sessions = [session("wf-a"), session("wf-b")];

void controller.selectSession("wf-a");
await waitFor("A attached", () =>
  attachCalls.some((c) => c.sessionId === "wf-a")
);
streams["wf-a"].push(
  ...Array.from({ length: 10 }, (_, i) =>
    variedFrame(i, { replay: false })
  )
);
await sleep(80);

/* Mid-stream switch: B must never show A's frames. */
void controller.selectSession("wf-b");
streams["wf-a"].push(
  ...Array.from({ length: 8 }, (_, i) =>
    variedFrame(100 + i, { replay: false })
  )
);
await waitFor("B attached", () =>
  attachCalls.some((c) => c.sessionId === "wf-b")
);
streams["wf-b"].push(
  ...Array.from({ length: 7 }, (_, i) =>
    variedFrame(i, { replay: false })
  )
);
await waitFor("B frames published", () => controller.frames.length >= 7);

assert.ok(
  controller.frames.every(
    (f) => f.data.agent_id === "root" || f.data.agent_id === "kid"
  )
);
/* B's burst used offsets 0..6; A's late frames used 100+ — reject A's agent
   markers if we stamped them. Varied frames share agent ids; stamp session. */
const bOffsets = new Set(
  controller.frames.map((f) => f.data.event_offset)
);
assert.ok(
  [...bOffsets].every((o) => o < 100),
  "B must not show A's late high-offset frames"
);
assert.equal(
  controller.session?.workflow_id,
  "wf-b",
  "active session must be B"
);

/* Rapid A→B→A under load; frames must not grow without bound. */
let maxFrames = controller.frames.length;
const attachBeforeLoop = counts.attach;

for (let i = 0; i < SWITCHES; i += 1) {
  const target = i % 2 === 0 ? "wf-a" : "wf-b";
  const other = target === "wf-a" ? "wf-b" : "wf-a";
  void controller.selectSession(target);
  await waitFor(
    `switch to ${target}`,
    () => controller.session?.workflow_id === target,
    6_000
  );
  streams[other].push(
    ...Array.from({ length: BURST }, (_, j) =>
      variedFrame(1_000 + i * BURST + j, { replay: false })
    )
  );
  streams[target].push(
    ...Array.from({ length: 3 }, (_, j) =>
      variedFrame(j, { replay: false })
    )
  );
  await sleep(40);
  maxFrames = Math.max(maxFrames, controller.frames.length);
}

assert.ok(
  maxFrames < CACHE_BOUND(),
  `frames must stay bounded across ${SWITCHES} switches (max ${maxFrames})`
);

/* Abandoned session's attaches must not keep growing forever after we settle. */
const attachMid = counts.attach;
await sleep(400);
assert.ok(
  counts.attach - attachMid < 8,
  `attach storm after settle (grew by ${counts.attach - attachMid})`
);
assert.ok(
  counts.attach - attachBeforeLoop < SWITCHES * 4 + 10,
  `attach starts across loop must stay bounded (saw ${counts.attach - attachBeforeLoop})`
);

streams["wf-a"].end();
streams["wf-b"].end();
await sleep(50);
await vite.close();
console.log(
  `soak-session-switch OK (${SWITCHES} switches, max frames ${maxFrames}, attaches ${counts.attach})`
);
process.exit(0);

function CACHE_BOUND() {
  /* Empty caches + small bursts; anything near SWITCHES*BURST is a leak. */
  return 200;
}
