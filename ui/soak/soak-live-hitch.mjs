/**
 * Live hitch soak — per-commit cost as frames grow under the live (rAF) path.
 *
 * Extends check-projection-cost (one-shot builders) with the live cadence:
 * frames stage, one #publishFrames per paint, then the UI reads $derived
 * projections. Total work is commits × frames; one commit must stay near-linear.
 *
 * Budgets (measured 2026-09-04 on a quiet arm64 laptop, Node 26):
 *   - 2.5k commit+touch ~8–15ms; 5k ~15–30ms (matches agentRun ~27ms comment)
 *   - CI-tolerant ceilings: ratio < 3× for 2× size; 5k commit < 150ms;
 *     5k burst ingest+one publish < 5s (sync-publish-per-frame would be far slower)
 *
 * Run: node ui/soak/soak-live-hitch.mjs
 */
import assert from "node:assert/strict";
import { assertRatio, assertUnder, bestOf } from "./lib/assertBudget.mjs";
import {
  installBrowserSurface,
  loadControllerModules,
  session,
  sleep,
  touchProjections,
  variedFrame,
  waitFor
} from "./lib/controllerHarness.mjs";
import { controllableStream, createMockApi } from "./lib/mockApi.mjs";

const N = 2_500;
const TWO_N = 5_000;
const RATIO_MAX = 3;
/** Quiet ~27ms class; CI-tolerant hard fail. */
const COMMIT_5K_MS = 150;
/** Burst ingest + single paint; sync-per-frame would blow this. */
const INGEST_5K_MS = 5_000;

const { flushRaf, freshStorage } = installBrowserSurface({
  controllableRaf: true
});
const { vite, AgentRunController } = await loadControllerModules(import.meta.url);

function boot(stream) {
  freshStorage();
  const { api, attachCalls } = createMockApi({
    sessions: () => [session("wf-hitch")],
    streamFor: () => stream
  });
  const controller = new AgentRunController(api);
  controller.sessions = [session("wf-hitch")];
  return { controller, attachCalls, stream };
}

async function ingestLive(controller, stream, attachCalls, n) {
  void controller.selectSession("wf-hitch");
  await waitFor("attach", () => attachCalls.length >= 1);
  stream.push(
    ...Array.from({ length: n }, (_, i) => variedFrame(i, { replay: false }))
  );
  await waitFor(
    `${n} frames ingested`,
    () => controller.lastResumeOffset >= n,
    30_000
  );
  assert.equal(
    controller.frames.length,
    0,
    "live frames must stay staged until the paint flush"
  );
}

async function measureCommit(n) {
  return bestOf(3, async () => {
    const stream = controllableStream();
    const { controller, attachCalls } = boot(stream);
    await ingestLive(controller, stream, attachCalls, n);
    const started = performance.now();
    flushRaf();
    touchProjections(controller);
    const ms = performance.now() - started;
    assert.equal(controller.frames.length, n);
    stream.end();
    return ms;
  });
}

/* Warm Vite + hidden classes before the clocked rounds. */
{
  const stream = controllableStream();
  const { controller, attachCalls } = boot(stream);
  await ingestLive(controller, stream, attachCalls, 500);
  flushRaf();
  touchProjections(controller);
  stream.end();
  await sleep(50);
}

const costN = await measureCommit(N);
const cost2N = await measureCommit(TWO_N);
const ratio = cost2N / costN;

assertRatio(
  ratio,
  RATIO_MAX,
  `${N}: ${costN.toFixed(1)}ms, ${TWO_N}: ${cost2N.toFixed(1)}ms`
);
assertUnder(cost2N, COMMIT_5K_MS, `live commit at ${TWO_N}`);

/* Burst ingest: all frames before one paint — must not sync-publish each. */
{
  const stream = controllableStream();
  const { controller, attachCalls } = boot(stream);
  const started = performance.now();
  await ingestLive(controller, stream, attachCalls, TWO_N);
  flushRaf();
  touchProjections(controller);
  const elapsed = performance.now() - started;
  assert.equal(controller.frames.length, TWO_N);
  assertUnder(elapsed, INGEST_5K_MS, `live ingest+publish of ${TWO_N}`);
  stream.end();
}

await vite.close();
console.log(
  `soak-live-hitch OK (${N} ${costN.toFixed(1)}ms, ${TWO_N} ${cost2N.toFixed(1)}ms, ${ratio.toFixed(2)}x)`
);
process.exit(0);
