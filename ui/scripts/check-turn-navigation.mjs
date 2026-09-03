// ABOUTME: Asserts the turn controls move, from the one position they are always reached from — the
// exact index of a turn marker. nextTurn() sought `marker.index >= viewIndex`, which matches the
// marker the cursor is standing on, so at the start of any turn it re-seeked to the index it was
// already on and the button did nothing. Both turn buttons leave you exactly there, so the dead
// position was the common one, not an edge.
//   node ui/scripts/check-turn-navigation.mjs
//
// Driven by the repo's own mock run rather than a fixture written for this file. The indices are
// whatever realisticQaScenario produces (14 markers over 144 frames, one of them at index 0, which
// is what makes the boundary reachable at all) and every assertion below is stated against
// `run.turnMarkers` as read back from the controller, so a scenario edit moves the check with it
// instead of quietly aiming it at indices that no longer hold markers.
//
// The `>=` -> `>` revert is what this file is calibrated against: with it restored, the per-marker
// case fails on the first marker and the forward walk exhausts its step budget without leaving
// index 0.
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

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

const storage = { local: memoryStorage(), session: memoryStorage() };
globalThis.window = {
  get localStorage() {
    return storage.local;
  },
  get sessionStorage() {
    return storage.session;
  },
  setTimeout: (...a) => setTimeout(...a),
  clearTimeout: (...a) => clearTimeout(...a),
  setInterval: (...a) => setInterval(...a),
  clearInterval: (...a) => clearInterval(...a),
  addEventListener: () => {},
  removeEventListener: () => {}
};
globalThis.localStorage = new Proxy({}, { get: (_, p) => storage.local[p] });
globalThis.sessionStorage = new Proxy({}, { get: (_, p) => storage.session[p] });
globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

/* Loaded through vite for the reason check-frame-key.mjs records: agentRun.svelte.ts uses runes and
   extensionless relative imports, so it needs compileModule() and a resolver, both of which vite
   already owns here. The point of paying that ~5s is that these are the shipped methods — a copy of
   nextTurn() in this file could agree with every assertion while the app drifted away from both. */
const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "silent"
});
const { AgentRunController } = await vite.ssrLoadModule("/src/lib/state/agentRun.svelte.ts");
const { realisticQaScenario } = await vite.ssrLoadModule("/src/lib/mock/scenarios.ts");

const api = new Proxy({}, { get: () => async () => [] });
const run = new AgentRunController(api);
run.sessions = realisticQaScenario.sessions;
run.session = realisticQaScenario.sessions[0];
run.frames = realisticQaScenario.frames;

const markers = run.turnMarkers.map((marker) => marker.index);
const total = run.total;

assert.ok(markers.length >= 3, `the scenario must carry turns to navigate (saw ${markers.length})`);
assert.equal(markers[0], 0, "a run's first turn starts at index 0, which is the boundary case");
assert.ok(markers.at(-1) < total, "the last marker must leave somewhere for the end fallback to go");

/* previousTurn() seeks `index < viewIndex - 1`, not `< viewIndex`, so it steps over a marker sitting
   one index behind the cursor. Unreachable here — the tightest gap in this scenario is 3 — but the
   backward assertions below would fail confusingly rather than informatively if a scenario edit ever
   put two turn_starteds on adjacent indices, so the precondition says so out loud. That asymmetry is
   left as found: it is a separate question from the boundary this file exists to pin. */
const gaps = markers.slice(1).map((index, i) => index - markers[i]);
assert.ok(
  Math.min(...gaps) >= 2,
  `these assertions assume turns start at least 2 indices apart (tightest gap ${Math.min(...gaps)})`
);

/* `following` is not a mode, and StepController's jump-to-latest button is disabled on it rather
   than announcing itself pressed, which is only honest if the flag means exactly "the cursor is at
   the end". goTo() assigns it that way, and every seek below routes through goTo, so the invariant
   is asserted after each one rather than trusted once. */
const invariantHolds = (where) =>
  assert.equal(
    run.following,
    run.viewIndex === total,
    `following must mean viewIndex === total (${where}: following=${run.following}, ` +
      `viewIndex=${run.viewIndex}, total=${total})`
  );

// --- the boundary: from a marker's own index, forward must reach the NEXT marker ---------------
// The whole bug, stated once per turn in the run. `>=` matches the marker under the cursor, so every
// one of these stays put.
for (let i = 0; i < markers.length - 1; i += 1) {
  run.goTo(markers[i]);
  assert.equal(run.viewIndex, markers[i], "the seek that sets up the case must itself land");
  run.nextTurn();
  assert.equal(
    run.viewIndex,
    markers[i + 1],
    `from the exact index of turn ${i + 1} (index ${markers[i]}), Next turn must advance to the ` +
      `following marker at ${markers[i + 1]} — it stayed at ${run.viewIndex}`
  );
  invariantHolds(`after nextTurn from marker ${markers[i]}`);
}

// --- past the last marker, forward runs to the end --------------------------------------------
// The `?? this.total` fallback. Also the one place nextTurn is expected to set `following`.
{
  run.goTo(markers.at(-1));
  run.nextTurn();
  assert.equal(run.viewIndex, total, "from the last turn, Next turn must run to the live edge");
  assert.equal(run.following, true, "arriving at the end is what following means");
  invariantHolds("after nextTurn from the last marker");
}

// --- from between two markers, forward still reaches the next one ------------------------------
// Passes under `>=` too, since a cursor mid-turn has no marker of its own to match. Pinned so a
// future rewrite cannot fix the boundary by breaking the ordinary case.
{
  const midway = markers[1] + 1;
  assert.ok(midway < markers[2], "the midway probe must stay inside turn 2");
  run.goTo(midway);
  run.nextTurn();
  assert.equal(run.viewIndex, markers[2], "from mid-turn, Next turn must reach the next turn");
}

// --- the forward walk: repeated presses must enumerate the run, not stall ----------------------
// The failure as a person meets it — holding Next turn down from the start of a session. Under `>=`
// this never leaves index 0, so the budget is the assertion: it cannot be spent if each press moves.
{
  run.goTo(0);
  const visited = [0];
  const budget = markers.length + 4;
  for (let press = 0; press < budget && run.viewIndex < total; press += 1) {
    const before = run.viewIndex;
    run.nextTurn();
    assert.ok(
      run.viewIndex > before,
      `press ${press + 1} of Next turn did not move the cursor off index ${before}`
    );
    visited.push(run.viewIndex);
  }
  assert.equal(
    run.viewIndex,
    total,
    `walking forward by turns must reach the end within ${budget} presses (stopped at ` +
      `${run.viewIndex} having visited ${JSON.stringify(visited)})`
  );
  assert.deepEqual(
    visited,
    [...markers, total],
    "the forward walk must land on every turn in order, then the live edge"
  );
}

// --- backward, from a marker's own index -------------------------------------------------------
// previousTurn() already had the strict form, so this is the arm that worked. Pinned because the two
// are only useful as a pair: the fix above is "make forward behave like backward", and that claim
// stops being checkable the moment backward is unpinned.
for (let i = markers.length - 1; i > 0; i -= 1) {
  run.goTo(markers[i]);
  run.previousTurn();
  assert.equal(
    run.viewIndex,
    markers[i - 1],
    `from the exact index of turn ${i + 1} (index ${markers[i]}), Previous turn must fall back to ` +
      `the marker at ${markers[i - 1]} — it went to ${run.viewIndex}`
  );
  invariantHolds(`after previousTurn from marker ${markers[i]}`);
}

// --- the backward walk, and the floor ----------------------------------------------------------
{
  run.goTo(total);
  const budget = markers.length + 4;
  for (let press = 0; press < budget && run.viewIndex > 0; press += 1) {
    const before = run.viewIndex;
    run.previousTurn();
    assert.ok(
      run.viewIndex < before,
      `press ${press + 1} of Previous turn did not move the cursor off index ${before}`
    );
  }
  assert.equal(run.viewIndex, 0, "walking back by turns must reach the first step");

  /* The `?? 0` floor: already home, and it stays there rather than wrapping. */
  run.previousTurn();
  assert.equal(run.viewIndex, 0, "Previous turn at the first step must stay at the first step");
  invariantHolds("at the floor");
}

// --- the pair round-trips ----------------------------------------------------------------------
// Only true when both bounds are strict: `>=` returns 0 for the forward leg and the round trip
// "succeeds" without either press having moved.
for (let i = 0; i < markers.length - 1; i += 1) {
  run.goTo(markers[i]);
  run.nextTurn();
  run.previousTurn();
  assert.equal(
    run.viewIndex,
    markers[i],
    `Next turn then Previous turn must return to index ${markers[i]}`
  );
}

/* Both controls pause: scrubbing by turns is reading, and playback would walk the cursor off
   whatever was being read. */
{
  run.goTo(0);
  run.play();
  assert.equal(run.playing, true, "the setup must actually be playing");
  run.nextTurn();
  assert.equal(run.playing, false, "Next turn must pause playback");
  run.play();
  run.previousTurn();
  assert.equal(run.playing, false, "Previous turn must pause playback");
  run.pause();
}

await vite.close();
console.log(
  `turn navigation: ok (${markers.length} turns over ${total} events, ` +
    `markers at ${markers.join(", ")})`
);
process.exit(0);
