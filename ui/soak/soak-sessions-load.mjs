/**
 * Sessions under load — lite poll stays cheap; full enrich only on revision / picker.
 *
 * Pins the client contract for GET /api/sessions?view=ids vs full enrich
 * (#35/#75-class in temporal_agent_harness/web/app.py) without a Temporal cluster.
 *
 * Run: node ui/soak/soak-sessions-load.mjs
 */
import assert from "node:assert/strict";
import {
  installBrowserSurface,
  loadControllerModules,
  session,
  sleep
} from "./lib/controllerHarness.mjs";
import { createMockApi } from "./lib/mockApi.mjs";

const SESSION_COUNT = 200;
const STABLE_SYNCS = 20;
const AGE_GATE_MS = 5_000;

const { freshStorage } = installBrowserSurface();
const { vite, AgentRunController } = await loadControllerModules(import.meta.url);

const rows = Array.from({ length: SESSION_COUNT }, (_, i) =>
  session(`wf-sess-${i}`, { label: `Session ${i}`, created_at: i + 1 })
);

let rev = "rev-stable";
const { api, counts } = createMockApi({
  sessions: () => rows,
  revision: () => rev
});

freshStorage();
const controller = new AgentRunController(api);

/* Seed revision via first enrich (picker / boot path). */
await controller.ensureSessionsEnriched(0);
assert.equal(controller.sessions.length, SESSION_COUNT);
assert.equal(counts.listSessions, 1);
assert.ok(counts.listSessionsExistence >= 1);

const existenceAfterSeed = counts.listSessionsExistence;
const fullAfterSeed = counts.listSessions;

/* N syncs with stable revision → N existence, 0 full. */
for (let i = 0; i < STABLE_SYNCS; i += 1) {
  await controller.syncSessions();
}
assert.equal(
  counts.listSessionsExistence,
  existenceAfterSeed + STABLE_SYNCS,
  "each syncSessions must hit existence"
);
assert.equal(
  counts.listSessions,
  fullAfterSeed,
  "stable revision must not re-enrich"
);

/* One revision bump → exactly one full enrich. */
rev = "rev-bumped";
await controller.syncSessions();
assert.equal(
  counts.listSessions,
  fullAfterSeed + 1,
  "revision bump must enrich exactly once"
);
assert.equal(
  counts.listSessionsExistence,
  existenceAfterSeed + STABLE_SYNCS + 2,
  "bump: sync existence + loadSessions existence"
);

const fullAfterBump = counts.listSessions;

/* Age-gate: second ensure within 5s does not re-hit full list. */
await controller.ensureSessionsEnriched(AGE_GATE_MS);
assert.equal(
  counts.listSessions,
  fullAfterBump,
  "fresh enrich must age-gate within maxAgeMs"
);

/* Advance wall clock past the gate. */
const realNow = Date.now;
let offset = 0;
Date.now = () => realNow() + offset;
try {
  offset = AGE_GATE_MS + 1;
  await controller.ensureSessionsEnriched(AGE_GATE_MS);
  assert.equal(
    counts.listSessions,
    fullAfterBump + 1,
    "enrich past maxAgeMs must hit full list"
  );
} finally {
  Date.now = realNow;
}

await sleep(10);
await vite.close();
console.log(
  `soak-sessions-load OK (${SESSION_COUNT} sessions, ${STABLE_SYNCS} stable syncs)`
);
process.exit(0);
