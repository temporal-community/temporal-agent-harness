// ABOUTME: Checks the replay hotkeys on both halves of a binding — when it is allowed to fire, and
// what it then does. The second half is why this file was rewritten: it used to assert only that
// two keys resolved to two different action *names*, which `End` and `L` did while both ran
// `goTo(total)` and landed on identical state. A name is not a behaviour, so the help overlay
// advertised two shortcuts that were one, and this check passed.
//   node ui/scripts/check-replay-hotkeys.mjs
//
// The last section covers what the transport row stopped offering: four buttons were cut on the
// argument that the keyboard does relative movement better, so the keys are now the only route and
// this is the only thing that would notice one going missing.
//
// The guards are pure decisions and are checked as such. The behaviour half drives the shipped
// `applyReplayAction` — the same function App.svelte's window handler calls — against a real
// `AgentRunController` filled from `realisticQaScenario`, and compares state read back from the
// controller rather than hardcoded indices. Every pair of rows in the overlay must be told apart
// by some starting position. Re-add `{ action: "jumpToLive", key: "l", ... }` and this file fails
// on that pair, naming both rows.
//
// A third thing is checked between those two: the keyboard seek count the Logs pane reads to
// scroll instantly for keys and smoothly for clicks. Measured as a delta over the shipped dispatch
// and the shipped run methods, so it fails if a key stops counting, if a key that moves nothing
// starts counting, or if the click path ever counts.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

/* Loaded through vite for the reason check-turn-navigation.mjs records: the state modules use
   runes and extensionless relative imports, so they need compileModule() and a resolver, both of
   which vite already owns here. One server for the whole file also means the table the guards read
   is the same object the actions below are driven from. */
const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "silent"
});
const { REPLAY_BINDINGS, resolveReplayAction, applyReplayAction, keyboardSeekCount } =
  await vite.ssrLoadModule("/src/lib/state/replayHotkeys.ts");

/** A key press on nothing in particular: no modifiers, no focused control. */
function press(overrides = {}) {
  return {
    key: "ArrowRight",
    shiftKey: false,
    modified: false,
    composing: false,
    typing: false,
    rangeFocused: false,
    spaceActivates: false,
    claimedKeys: [],
    ...overrides
  };
}

// A plain key does what the table says.
assert.equal(resolveReplayAction(press({ key: "ArrowRight" })), "stepForward");
assert.equal(resolveReplayAction(press({ key: "ArrowLeft" })), "stepBack");
assert.equal(
  resolveReplayAction(press({ key: "ArrowRight", shiftKey: true })),
  "nextTurn",
  "Shift picks the turn-sized jump, not the frame-sized one"
);
assert.equal(resolveReplayAction(press({ key: "Home" })), "first");
assert.equal(resolveReplayAction(press({ key: "End" })), "last");
assert.equal(resolveReplayAction(press({ key: " " })), "togglePlay");
assert.equal(resolveReplayAction(press({ key: "?" })), "toggleHelp");
assert.equal(resolveReplayAction(press({ key: "q" })), null, "unbound keys do nothing");

// Nothing fires while the user is typing. This is the property that matters.
for (const key of ["ArrowLeft", "ArrowRight", "Home", "End", " ", "?"]) {
  assert.equal(
    resolveReplayAction(press({ key, typing: true })),
    null,
    `${JSON.stringify(key)} must not act while focus is in a text field`
  );
  assert.equal(
    resolveReplayAction(press({ key, composing: true })),
    null,
    `${JSON.stringify(key)} must not act while an IME is composing`
  );
}

// Browser and OS chords are left alone.
assert.equal(resolveReplayAction(press({ key: "ArrowRight", modified: true })), null);
assert.equal(resolveReplayAction(press({ key: " ", modified: true })), null);

// The focused scrubber keeps its free native stepping, with no second step.
for (const key of ["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"]) {
  assert.equal(
    resolveReplayAction(press({ key, rangeFocused: true })),
    null,
    `${key} on the focused scrubber must be left to the native range input`
  );
}
assert.equal(
  resolveReplayAction(press({ key: "?", rangeFocused: true })),
  "toggleHelp",
  "keys the range input ignores still work while it is focused"
);

// A control that declares its keys keeps them.
assert.equal(
  resolveReplayAction(press({ key: "Home", claimedKeys: ["ArrowLeft", "ArrowRight", "Home", "End"] })),
  null,
  "aria-keyshortcuts on the focused element shadows the global binding"
);
assert.equal(
  resolveReplayAction(press({ key: "ArrowLeft", claimedKeys: ["Shift+ArrowLeft"] })),
  null,
  "a claim is matched on its key, whatever modifiers it names"
);
assert.equal(
  resolveReplayAction(press({ key: "ArrowRight", claimedKeys: ["Home", "End"] })),
  "stepForward",
  "unclaimed keys still reach the global binding"
);

// Space activates a focused button; it must not also toggle playback.
assert.equal(resolveReplayAction(press({ key: " ", spaceActivates: true })), null);
assert.equal(
  resolveReplayAction(press({ key: "ArrowRight", spaceActivates: true })),
  "stepForward",
  "a focused button only shadows the key that activates it"
);

// The overlay renders straight from the table, so every row needs a legible
// chord and label, and no two bindings may claim the same chord.
const chords = new Set();
for (const binding of REPLAY_BINDINGS) {
  assert.ok(binding.chord.length > 0, `${binding.action} needs a chord to display`);
  assert.ok(binding.label.length > 0, `${binding.action} needs a label to display`);
  assert.ok(!chords.has(binding.chord), `duplicate chord in the help overlay: ${binding.chord}`);
  chords.add(binding.chord);
}

// --- what the keys actually do -----------------------------------------------------------------

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

const { AgentRunController } = await vite.ssrLoadModule("/src/lib/state/agentRun.svelte.ts");
const { realisticQaScenario } = await vite.ssrLoadModule("/src/lib/mock/scenarios.ts");

const api = new Proxy({}, { get: () => async () => [] });
const run = new AgentRunController(api);
run.sessions = realisticQaScenario.sessions;
run.session = realisticQaScenario.sessions[0];
run.frames = realisticQaScenario.frames;

const total = run.total;
const markers = run.turnMarkers.map((marker) => marker.index);
assert.ok(total > 4, `the scenario must carry a run to move around in (saw ${total} events)`);
assert.ok(markers.length >= 3, `the scenario must carry turns to navigate (saw ${markers.length})`);

/* The overlay flag lives in App.svelte and is passed to the action the same way here: a surface
   the action writes through. That is what keeps `?` and `Esc` inside this comparison. */
const surface = { run, helpOpen: false };

/* Positions a person can be standing at when they reach for a key, stated against the markers the
   scenario actually produces. Two bindings only have to differ somewhere, not everywhere — `Home`
   and `←` agree from index 1, which is fine — so a pair is condemned only when no position here
   tells them apart. */
const positions = [
  ["at the first event", () => run.goTo(0)],
  ["one event in", () => run.goTo(1)],
  ["at the start of a turn", () => run.goTo(markers[1])],
  ["mid-turn", () => run.goTo(markers[1] + 1)],
  ["at the start of a later turn", () => run.goTo(markers[2])],
  ["at the last turn", () => run.goTo(markers.at(-1))],
  ["one before the live edge", () => run.goTo(total - 1)],
  ["at the live edge", () => run.goTo(total)],
  [
    "playing, mid-run",
    () => {
      run.goTo(markers[1]);
      run.play();
    }
  ]
];
const probes = positions.flatMap(([where, seek]) =>
  [false, true].map((helpOpen) => [
    `${where}${helpOpen ? ", overlay open" : ""}`,
    () => {
      run.pause();
      seek();
      surface.helpOpen = helpOpen;
    }
  ])
);

const state = () => ({
  viewIndex: run.viewIndex,
  following: run.following,
  playing: run.playing,
  playbackSpeed: run.playbackSpeed,
  helpOpen: surface.helpOpen
});

/* One press of `action` from `probe`, measured. Playback is stopped straight after so the 700ms
   auto-advance timer can never fire between two probes and make this file flaky. */
function outcome([, setUp], action) {
  setUp();
  const before = JSON.stringify(state());
  applyReplayAction(action, surface);
  const after = JSON.stringify(state());
  run.pause();
  return { before, after };
}

/* The defect, stated directly: the one live-edge binding lands on the end, follows it, and stops
   playback. This is the whole of what `L` used to promise separately. */
{
  const [, setUp] = probes[0];
  setUp();
  applyReplayAction("last", surface);
  assert.deepEqual(state(), {
    viewIndex: total,
    following: true,
    playing: false,
    playbackSpeed: run.playbackSpeed,
    helpOpen: false
  });
}

// --- keys are told apart from clicks ----------------------------------------------------------

/* The Logs pane scrolls instantly when a key moved the playhead and smoothly when a click did,
   and the only thing it has to tell them apart is this count. So the count is checked on the
   property the pane depends on — it advances for keys that move, and for nothing else. Asserted
   as deltas through the shipped `applyReplayAction` and the shipped run methods, because "the
   transport button path" is nothing more than calling those methods without going through it. */
function seekDelta(move) {
  const before = keyboardSeekCount();
  move();
  run.pause();
  return keyboardSeekCount() - before;
}

const markerMid = markers[1];

// A key that moves the cursor is one seek, once.
run.goTo(markerMid);
assert.equal(seekDelta(() => applyReplayAction("stepForward", surface)), 1, "→ that moves counts");
assert.equal(seekDelta(() => applyReplayAction("stepBack", surface)), 1, "← that moves counts");
run.goTo(markerMid);
assert.equal(seekDelta(() => applyReplayAction("nextTurn", surface)), 1);
assert.equal(seekDelta(() => applyReplayAction("previousTurn", surface)), 1);
assert.equal(seekDelta(() => applyReplayAction("first", surface)), 1);
assert.equal(seekDelta(() => applyReplayAction("last", surface)), 1);

/* A key that lands where the cursor already was must leave no mark, or the next *click* inherits
   it and scrolls instantly. `←` at the first event and `→` at the live edge are where a reader
   holding a key ends up, so this is the ordinary case, not an exotic one. */
run.goTo(0);
assert.equal(
  seekDelta(() => applyReplayAction("stepBack", surface)),
  0,
  "← at the first event moves nothing and must not arm the next click"
);
assert.equal(seekDelta(() => applyReplayAction("first", surface)), 0, "Home when already first");
run.goTo(total);
assert.equal(
  seekDelta(() => applyReplayAction("stepForward", surface)),
  0,
  "→ at the live edge moves nothing and must not arm the next click"
);
assert.equal(seekDelta(() => applyReplayAction("last", surface)), 0, "End when already at the end");

// Keys that are not about position at all.
run.goTo(markerMid);
assert.equal(seekDelta(() => applyReplayAction("toggleHelp", surface)), 0, "? moves nothing");
assert.equal(seekDelta(() => applyReplayAction("closeHelp", surface)), 0, "Esc moves nothing");
assert.equal(
  seekDelta(() => applyReplayAction("togglePlay", surface)),
  0,
  "Space mid-run starts playback where the cursor already is"
);

/* The click path, which is every transport control and the scrubber: the same methods, reached
   without a key. None of them may count, or clicking would scroll instantly too and the smooth
   follow-along this whole distinction exists to keep would be gone. */
run.goTo(markerMid);
assert.equal(seekDelta(() => run.stepForward()), 0, "the step-forward button is not a key");
assert.equal(seekDelta(() => run.stepBack()), 0, "the step-back button is not a key");
assert.equal(seekDelta(() => run.nextTurn()), 0, "the next-turn button is not a key");
assert.equal(seekDelta(() => run.previousTurn()), 0, "the previous-turn button is not a key");
assert.equal(seekDelta(() => run.goTo(markers[2])), 0, "dragging the scrubber is not a key");
assert.equal(seekDelta(() => run.jumpToLive()), 0, "the live button is not a key");
/* What the playback timer does every 700ms once Space has been pressed. Following the live edge
   is exactly the case the smooth scroll is for, so those frames must not read as keyboard. */
run.goTo(markerMid);
run.play();
assert.equal(seekDelta(() => run.stepForward()), 0, "playback advancing is not a key press");
run.pause();
console.log("  keyboard seeks counted on moving keys only, never on the click path");

/* `following` means "the cursor is at the end" and nothing else: goTo() assigns it that way and
   every transport action routes through goTo, so it is asserted after every press below rather
   than trusted once. */
for (const probe of probes) {
  for (const binding of REPLAY_BINDINGS) {
    outcome(probe, binding.action);
    assert.equal(
      run.following,
      run.viewIndex === total,
      `following must mean viewIndex === total (${binding.chord} ${probe[0]}: ` +
        `following=${run.following}, viewIndex=${run.viewIndex}, total=${total})`
    );
  }
}

/* The check the old one should have been. Every row in the overlay promises a behaviour of its
   own; a row that cannot be told from another row promises something the app does not have.
   Compared by effect, so what the two actions are named does not enter into it. */
for (let i = 0; i < REPLAY_BINDINGS.length; i += 1) {
  for (let j = i + 1; j < REPLAY_BINDINGS.length; j += 1) {
    const [a, b] = [REPLAY_BINDINGS[i], REPLAY_BINDINGS[j]];
    assert.ok(
      probes.some((probe) => outcome(probe, a.action).after !== outcome(probe, b.action).after),
      `"${a.chord} — ${a.label}" and "${b.chord} — ${b.label}" leave the replay in the same ` +
        `state from all ${probes.length} starting positions: one behaviour, two rows in the help ` +
        `overlay. Drop a binding, or merge them into a single row that lists both keys.`
    );
  }
}

/* A key that changes nothing anywhere is the same broken promise with one row instead of two. */
for (const binding of REPLAY_BINDINGS) {
  assert.ok(
    probes.some((probe) => {
      const { before, after } = outcome(probe, binding.action);
      return before !== after;
    }),
    `"${binding.chord} — ${binding.label}" never changes anything the user can see`
  );
}

// --- motions the transport row no longer offers a button for -------------------------------------
// The row was cut from seven buttons to three, on the argument that relative movement — one event
// or one turn, either direction — is better done from the keyboard. That argument is only true
// while the keys work, and nothing else in this repo would notice if one stopped: the four buttons
// that used to be the fallback are gone.
//
// So the assertion is the consequence, not the shape. For each motion the row dropped, this
// demands a binding that (a) a real key press resolves to, through the same guards the window
// handler runs, and (b) leaves the run in the state the removed button's own handler did, from
// every starting position above. Compared by effect, so renaming an action changes nothing here.
//
// Drop the `previousTurn` row from REPLAY_BINDINGS, or give it a key the guards swallow, and this
// fails naming "one turn back" — which is exactly the silence the deleted buttons used to cover.

/** The same measurement as `outcome`, for a motion invoked directly rather than through a key. */
function effectOf([, setUp], act) {
  setUp();
  act();
  const after = JSON.stringify(state());
  run.pause();
  return after;
}

/* `label="..."` is the IconButton prop; the negative lookbehind keeps `aria-label=` out. */
const controller = await readFile(
  new URL("../src/lib/components/flow/StepController.svelte", import.meta.url),
  "utf8"
);
const buttonLabels = new Set([...controller.matchAll(/(?<!aria-)label="([^"]+)"/g)].map((m) => m[1]));
assert.ok(
  buttonLabels.size > 0,
  "found no IconButton labels in StepController.svelte — the parse above has gone stale"
);

/* Each entry is the button that was removed and the controller call it made, taken from the
   handler App.svelte used to pass it. `previousTurn`/`nextTurn` pause on their own, which is why
   the extra `run.pause()` those handlers carried is not repeated here. */
const CUT_MOTIONS = [
  { motion: "one event back", button: "Previous event", act: () => run.stepBack() },
  { motion: "one event forward", button: "Next event", act: () => run.stepForward() },
  { motion: "one turn back", button: "Previous turn", act: () => run.previousTurn() },
  { motion: "one turn forward", button: "Next turn", act: () => run.nextTurn() }
];

for (const { motion, button, act } of CUT_MOTIONS) {
  assert.ok(
    !buttonLabels.has(button),
    `StepController.svelte still renders a "${button}" button. Either the row grew back — in ` +
      `which case drop this entry — or the label drifted and this check is now guarding nothing.`
  );

  const reachable = REPLAY_BINDINGS.filter((binding) => {
    /* The key must survive the guards on the way in. `shift: null` means the binding does not
       care, and the plain press is the one a person makes. */
    const resolved = resolveReplayAction(
      press({ key: binding.key, shiftKey: binding.shift === true })
    );
    if (resolved !== binding.action) return false;
    return probes.every(
      (probe) => outcome(probe, binding.action).after === effectOf(probe, act)
    );
  });

  assert.ok(
    reachable.length > 0,
    `"${button}" is gone from the transport row and no key reproduces it: ${motion} is now ` +
      `unreachable. Over all ${probes.length} starting positions, no binding in REPLAY_BINDINGS ` +
      `both resolves from its own key press and lands where that button did.`
  );
  console.log(`  ${motion.padEnd(18)} button gone, reached by ${reachable[0].chord}`);
}

run.pause();
await vite.close();
console.log(
  `replay hotkeys: ${REPLAY_BINDINGS.length} bindings, guards hold, every pair distinguishable ` +
    `over ${probes.length} starting positions (${total} events, ${markers.length} turns); ` +
    `${CUT_MOTIONS.length} button-less motions still reachable by key`
);
process.exit(0);
