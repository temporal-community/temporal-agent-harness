/**
 * Catch-up / cache reopen — large cached hydrate stays chunked; connecting clears.
 *
 * Pre-seeds ~3k frames via writeCachedFrames, selects the session, attach ends
 * empty while RUNNING (caught-up shape from check-caught-up-attach.mjs).
 *
 * Budgets: connecting must clear in seconds (≪ ~31.5s retry budget). Publish
 * count stays ≪ frame count (chunk / ceiling path, not one commit per frame).
 *
 * Run: node ui/soak/soak-catchup-reopen.mjs
 */
import assert from "node:assert/strict";
import { assertUnder } from "./lib/assertBudget.mjs";
import {
  installBrowserSurface,
  loadControllerModules,
  session,
  sleep,
  variedFrame,
  waitFor
} from "./lib/controllerHarness.mjs";
import { controllableStream, createMockApi, silentStream } from "./lib/mockApi.mjs";

const CACHE_N = 3_000;
/** ≪ 31.5s reattach budget; quiet machines clear in tens–hundreds of ms. */
const CONNECTING_MS = 3_000;

const { freshStorage } = installBrowserSurface();
const {
  vite,
  AgentRunController,
  writeCachedFrames,
  framePublishChunkSize
} = await loadControllerModules(import.meta.url);

const cached = Array.from({ length: CACHE_N }, (_, i) =>
  variedFrame(i, { replay: true })
);

const liveStream = controllableStream();
let attachCount = 0;

freshStorage();
writeCachedFrames("wf-cache", cached);

const { api, attachCalls } = createMockApi({
  sessions: () => [session("wf-cache")],
  streamFor: () => {
    attachCount += 1;
    /* First attach: caught-up empty. Later: live frames for latch check. */
    if (attachCount === 1) return silentStream();
    return liveStream;
  }
});

const controller = new AgentRunController(api);
controller.sessions = [session("wf-cache")];

let publishEvents = 0;
let lastLen = 0;
const watch = setInterval(() => {
  const len = controller.frames.length;
  if (len !== lastLen) {
    publishEvents += 1;
    lastLen = len;
  }
}, 5);

const started = Date.now();
void controller.selectSession("wf-cache");
await waitFor("connecting to clear", () => !controller.connecting, CONNECTING_MS);
const connectingMs = Date.now() - started;
assertUnder(connectingMs, CONNECTING_MS, "time-to-connecting-false");

await waitFor(
  "cache hydrate to finish",
  () => controller.frames.length === CACHE_N,
  30_000
);
clearInterval(watch);

assert.equal(controller.frames.length, CACHE_N, "final frames must match cache");

const maxPublishes = Math.ceil(CACHE_N / framePublishChunkSize) + 2;
assert.ok(
  publishEvents <= maxPublishes,
  `hydrate must publish in chunks, not per frame (saw ${publishEvents} publishes, max ${maxPublishes})`
);
assert.ok(
  publishEvents < CACHE_N / 10,
  `hydrate must not sync-commit each frame (saw ${publishEvents} for ${CACHE_N} frames)`
);

/* First live frame after replay clears catch-up latch — appends and publishes. */
await waitFor("re-attach after catch-up", () => attachCalls.length >= 2, 6_000);
liveStream.push(variedFrame(CACHE_N, { replay: false }));
await waitFor(
  "live frame after cache",
  () => controller.frames.length === CACHE_N + 1,
  6_000
);
assert.equal(
  controller.frames.at(-1)?.data?.event_offset,
  CACHE_N,
  "live frame must land after the cached backlog"
);

liveStream.end();
await sleep(50);
await vite.close();
console.log(
  `soak-catchup-reopen OK (${CACHE_N} frames, connecting ${connectingMs}ms, ${publishEvents} publishes)`
);
process.exit(0);
