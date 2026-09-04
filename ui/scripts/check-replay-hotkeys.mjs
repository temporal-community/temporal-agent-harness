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
import * as fs from "node:fs/promises";
import { createCheckServer } from "./checkServer.mjs";

/* Loaded through vite for the reason check-turn-navigation.mjs records: the state modules use
   runes and extensionless relative imports, so they need compileModule() and a resolver, both of
   which vite already owns here. One server for the whole file also means the table the guards read
   is the same object the actions below are driven from. */
const vite = await createCheckServer(import.meta.url);
const {
  REPLAY_BINDINGS,
  describeReplayKeyEvent,
  resolveReplayAction,
  applyReplayAction,
  keyboardSeekCount,
  noteKeyboardSeek
} = await vite.ssrLoadModule("/src/lib/state/replayHotkeys.ts");

/** A key press on nothing in particular: no modifiers, no focused control. */
function press(overrides = {}) {
  return {
    key: "ArrowRight",
    shiftKey: false,
    altKey: false,
    modKey: false,
    composing: false,
    typing: false,
    spaceActivates: false,
    claimedKeys: [],
    helpOpen: false,
    bleeding: false,
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
assert.equal(resolveReplayAction(press({ key: "." })), "nextStep");
assert.equal(resolveReplayAction(press({ key: "," })), "previousStep");

/* The pane keys, which used to be an if-chain in App.svelte and are rows in the same table now.
   This block is the point of that move: before it, nothing in this repo pressed these keys. */
assert.equal(resolveReplayAction(press({ key: "ArrowLeft", altKey: true })), "railFocusPrevious");
assert.equal(resolveReplayAction(press({ key: "ArrowRight", altKey: true })), "railFocusNext");
assert.equal(resolveReplayAction(press({ key: "ArrowUp", altKey: true })), "railFocusPreviousTab");
assert.equal(resolveReplayAction(press({ key: "ArrowDown", altKey: true })), "railFocusNextTab");
assert.equal(
  resolveReplayAction(press({ key: "ArrowLeft", modKey: true, shiftKey: true })),
  "railMovePrevious"
);
assert.equal(
  resolveReplayAction(press({ key: "ArrowDown", modKey: true, shiftKey: true })),
  "railMoveNextTab"
);
assert.equal(resolveReplayAction(press({ key: "f" })), "railToggleBleed");
assert.equal(resolveReplayAction(press({ key: "F" })), "railToggleBleed", "caps lock is not Shift");
assert.equal(resolveReplayAction(press({ key: "f", modKey: true })), null, "Cmd+F is browser find");
assert.equal(resolveReplayAction(press({ key: "f", altKey: true })), null, "Alt+F is a menu");

/* Escape resolves against what is actually on screen, which is why it is one row and not two.
   With neither surface up the key is nobody's and the browser keeps it — a Escape that is always
   swallowed is one that cannot cancel a drag or dismiss a native prompt. */
assert.equal(resolveReplayAction(press({ key: "Escape" })), null, "nothing to escape from");
assert.equal(resolveReplayAction(press({ key: "Escape", helpOpen: true })), "escape");
assert.equal(resolveReplayAction(press({ key: "Escape", bleeding: true })), "escape");

/* Nothing fires while the user is typing, and "nothing" now means the pane keys too.
   Option+Left inside the chat composer is the OS's "back one word" — the console taking it was
   the bug that started this, and it reached the user because these keys were in an if-chain no
   check could see. Swept over the whole table rather than a list of keys, so a binding added
   later cannot quietly skip the guard. */
for (const binding of REPLAY_BINDINGS) {
  const typed = press({
    key: binding.key,
    shiftKey: binding.shift === true,
    altKey: binding.alt === true,
    modKey: binding.mod === true,
    helpOpen: true,
    bleeding: true
  });
  assert.equal(
    resolveReplayAction({ ...typed, typing: true }),
    null,
    `${binding.chord} must not act while focus is in a text field, a textarea or a contenteditable`
  );
  assert.equal(
    resolveReplayAction({ ...typed, composing: true }),
    null,
    `${binding.chord} must not act while an IME is composing`
  );
  /* And the same chord away from a field must reach its action, or the assertion above is
     passing because the binding is broken rather than because the guard works. */
  assert.equal(
    resolveReplayAction(typed),
    binding.action,
    `${binding.chord} must still resolve when focus is not in a text field`
  );
}

// Browser and OS chords the table does not spell are left alone.
assert.equal(resolveReplayAction(press({ key: "ArrowRight", modKey: true })), null);
assert.equal(resolveReplayAction(press({ key: " ", modKey: true })), null);
assert.equal(resolveReplayAction(press({ key: " ", altKey: true })), null);
assert.equal(
  resolveReplayAction(press({ key: "ArrowRight", altKey: true, modKey: true })),
  null,
  "Alt+Cmd+Right is the OS's, not the rail's"
);

/* The focused scrubber, which is the control a reader is most likely to be standing on when they
   want to traverse the timeline, and which used to be the one place traversal stopped working.
   `input.scrub-input` is a range, and a range is not text entry — if it ever starts reading as
   typing, the guard above declines the whole table and the timeline goes dead under the user's
   hands. Built from the shipped `describeReplayKeyEvent` against an element shaped like the real
   scrubber, so this pins the classification and not just a hand-written flag. */
const scrubber = {
  tagName: "INPUT",
  type: "range",
  isContentEditable: false,
  getAttribute: (name) => (name === "aria-label" ? "Replay position" : null)
};
const onScrubber = (event) =>
  describeReplayKeyEvent(
    { shiftKey: false, altKey: false, ctrlKey: false, metaKey: false, ...event, target: scrubber },
    { helpOpen: false, bleeding: false }
  );

assert.equal(
  onScrubber({ key: "ArrowRight" }).typing,
  false,
  "a range input holds no text, so a keystroke over the scrubber is not typing"
);
for (const binding of REPLAY_BINDINGS) {
  const context = {
    ...onScrubber({
      key: binding.key,
      shiftKey: binding.shift === true,
      altKey: binding.alt === true,
      ctrlKey: binding.mod === true
    }),
    helpOpen: true,
    bleeding: true
  };
  assert.equal(
    resolveReplayAction(context),
    binding.action,
    `${binding.chord} must still act with the scrub slider focused — that is where the user is`
  );
}

/* Where the table has no row, the slider keeps the key. This is the whole of the range deference
   now: not a list of keys to defer on, just the absence of a binding. `↑`/`↓` step it and PageUp/
   PageDown jump it by a tenth, which is how a screen-reader user drives a slider, and none of the
   four are chords this console spends. */
for (const key of ["ArrowUp", "ArrowDown", "PageUp", "PageDown"]) {
  assert.equal(
    resolveReplayAction(onScrubber({ key })),
    null,
    `${key} is the range input's own stepping and the table must not take it`
  );
}
/* The keys the table *does* spell are taken from the slider on purpose, and land in the same place
   its native stepping would: `←` is one event either way. The caller cancels the native step when a
   row resolves, so they never both apply — ui/tools/scrub-focus-keys.mjs measures that end to end. */
assert.equal(
  resolveReplayAction(onScrubber({ key: "ArrowLeft" })),
  "stepBack",
  "one event back, whichever path moves it"
);
assert.equal(
  resolveReplayAction(onScrubber({ key: "ArrowLeft", shiftKey: true })),
  "previousTurn",
  "Shift+Left is a turn, and a range input cannot tell it from a plain Left — this was the bug"
);

assert.equal(
  resolveReplayAction(
    press({ key: "ArrowLeft", altKey: true, claimedKeys: ["ArrowLeft", "ArrowRight", "Home", "End"] })
  ),
  "railFocusPrevious",
  "a control's aria-keyshortcuts claim covers the bare key, not the rail's chord over it"
);
assert.equal(
  resolveReplayAction(
    press({ key: "ArrowLeft", shiftKey: true, claimedKeys: ["ArrowLeft", "ArrowRight", "Home"] })
  ),
  "previousTurn",
  "the resizer hands back everything modified, Shift included, so a claim must not eat Shift+Left"
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

/**
 * A pane rail reduced to what the four `RailSurface` verbs do to one: columns of panes, a cursor
 * over them, and one pane that can be full-screen.
 *
 * A stub rather than a real `PaneStack` for the same reason `RailSurface` is not `PaneStack`:
 * there are two stacks on screen and which one a key acts on is App.svelte's question. What has to
 * be checked here is that eight rail bindings do eight different things, and that is measurable
 * against any rail at all.
 */
function stubRail() {
  const clamp = (value, limit) => Math.max(0, Math.min(value, limit));
  const rail = {
    columns: [],
    column: 0,
    tab: 0,
    bleeding: null,
    reset({ column, tab }) {
      rail.columns = [["a1", "a2"], ["b1"], ["c1", "c2", "c3"]];
      rail.column = column;
      rail.tab = tab;
      rail.bleeding = null;
    },
    focusedId: () => rail.columns[rail.column]?.[rail.tab] ?? null,
    focus(axis, delta) {
      if (axis === "along") {
        rail.column = clamp(rail.column + delta, rail.columns.length - 1);
        rail.tab = 0;
        return;
      }
      rail.tab = clamp(rail.tab + delta, rail.columns[rail.column].length - 1);
    },
    move(axis, delta) {
      const id = rail.focusedId();
      if (!id) return;
      if (axis === "along") {
        /* Carry the pane into the neighbouring column, which is what movePane does. */
        const to = clamp(rail.column + delta, rail.columns.length - 1);
        if (to === rail.column) return;
        rail.columns[rail.column].splice(rail.tab, 1);
        rail.columns[to].push(id);
        if (rail.columns[rail.column].length === 0) rail.columns.splice(rail.column, 1);
        rail.column = rail.columns.findIndex((column) => column.includes(id));
        rail.tab = rail.columns[rail.column].indexOf(id);
        return;
      }
      const column = rail.columns[rail.column];
      const to = clamp(rail.tab + delta, column.length - 1);
      if (to === rail.tab) return;
      column.splice(rail.tab, 1);
      column.splice(to, 0, id);
      rail.tab = to;
    },
    toggleBleed() {
      rail.bleeding = rail.bleeding === rail.focusedId() ? null : rail.focusedId();
    },
    exitBleed() {
      rail.bleeding = null;
    }
  };
  rail.reset({ column: 0, tab: 0 });
  return rail;
}

const rail = stubRail();

/* The overlay flag lives in App.svelte and is passed to the action the same way here: a surface
   the action writes through. That is what keeps `?`, `Esc` and the pane keys inside this
   comparison. */
const surface = { run, helpOpen: false, rail };

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
/* Rail positions, cycled through the probe list rather than multiplied into it. Eight rail
   bindings need somewhere to differ — `Alt ↑` and `Cmd Shift ↑` both do nothing on the first tab
   of a column, so a rail parked at one corner would report them identical — and the run positions
   above already number eighteen. */
const RAIL_STARTS = [
  { column: 0, tab: 0 },
  { column: 1, tab: 0 },
  { column: 2, tab: 1 },
  { column: 2, tab: 2 }
];

const probes = positions
  .flatMap(([where, seek]) =>
    [false, true].map((helpOpen) => [`${where}${helpOpen ? ", overlay open" : ""}`, seek, helpOpen])
  )
  .map(([where, seek, helpOpen], index) => {
    const railStart = RAIL_STARTS[index % RAIL_STARTS.length];
    return [
      `${where}, rail at ${railStart.column}.${railStart.tab}`,
      () => {
        run.pause();
        seek();
        surface.helpOpen = helpOpen;
        rail.reset(railStart);
      }
    ];
  });

const state = () => ({
  viewIndex: run.viewIndex,
  following: run.following,
  playing: run.playing,
  playbackSpeed: run.playbackSpeed,
  helpOpen: surface.helpOpen,
  /* The rail is part of the state a key can change, so it is part of what tells two keys apart. */
  rail: { columns: rail.columns, column: rail.column, tab: rail.tab, bleeding: rail.bleeding }
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
  const { rail: railAfter, ...transport } = state();
  assert.deepEqual(transport, {
    viewIndex: total,
    following: true,
    playing: false,
    playbackSpeed: run.playbackSpeed,
    helpOpen: false
  });
  assert.equal(railAfter.bleeding, null, "a transport key must not touch the desk");
}

// --- the step-sized jump ------------------------------------------------------------------------
// `,` and `.` exist because the transport had a hole in the middle of it: `←`/`→` move one frame,
// which nobody is looking for, and `Shift`+those move a whole turn, which in a real run is most of
// it. The middle rung is a span edge — a model call, a tool call or an approval starting or ending
// — which is not a new notion invented for the key: a span is what the state-flow graph draws as a
// card and what the latency waterfall draws as a bar, so this walks between the things already on
// screen.
//
// Asserted against the spans themselves rather than against remembered indices, so the day the
// projection changes what counts as a span, this either still holds or says which part stopped.
{
  const spans = run.stepTimeline.turns.flatMap((turn) => [
    ...turn.spans,
    ...turn.subagentTurns.flatMap((sub) => sub.spans)
  ]);
  const edges = new Set(spans.flatMap((span) => [span.startIndex, span.endIndex]));
  const boundaries = run.stepBoundaries;

  assert.ok(spans.length > 3, `the scenario must carry spans to jump between (saw ${spans.length})`);
  assert.deepEqual(
    boundaries,
    [...edges].sort((a, b) => a - b),
    "the boundary list must be every span edge, ascending and deduplicated, and nothing else"
  );
  assert.deepEqual(boundaries, [...new Set(boundaries)], "a boundary may not appear twice");

  /* Every landing is on a real boundary, from every event in the run — not from a handful of
     positions that happen to work. This is the assertion that would fail on an off-by-one in
     either direction, or on a `>=` that re-seeks where the cursor already is. */
  for (let from = 0; from <= total; from += 1) {
    run.goTo(from);
    run.nextStep();
    const forward = run.viewIndex;
    assert.ok(
      forward > from || forward === total,
      `. at ${from} went to ${forward}: forward must move forward, or stop at the live edge`
    );
    assert.ok(
      boundaries.includes(forward) || forward === total,
      `. at ${from} landed on ${forward}, which is not a span boundary`
    );

    run.goTo(from);
    run.previousStep();
    const back = run.viewIndex;
    assert.ok(back < from || from === 0, `, at ${from} went to ${back}: back must move back`);
    assert.ok(
      boundaries.includes(back) || back === 0,
      `, at ${from} landed on ${back}, which is not a span boundary`
    );
  }

  /* And it is genuinely the middle rung. Compared as totals over the whole run, because any one
     position can tie — a boundary that happens to sit one event along, a turn whose first span
     opens on its first frame. What must hold is that a reader crossing the run by span presses
     the key more often than by turn and less often than by frame. */
  const presses = (step) => {
    let count = 0;
    run.goTo(0);
    while (run.viewIndex < total && count <= total + 1) {
      const before = run.viewIndex;
      step();
      if (run.viewIndex === before) break;
      count += 1;
    }
    return count;
  };
  const byTurn = presses(() => run.nextTurn());
  const byStep = presses(() => run.nextStep());
  const byEvent = total;

  assert.ok(
    byTurn < byStep && byStep < byEvent,
    `. must be coarser than an event and finer than a turn, and over ${total} events it took ` +
      `${byEvent} presses by event, ${byStep} by step and ${byTurn} by turn`
  );
  console.log(
    `  crossing ${total} events takes ${byEvent} presses by event, ${byStep} by step ` +
      `(${boundaries.length} span boundaries), ${byTurn} by turn`
  );
  run.goTo(0);
  run.pause();
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
assert.equal(seekDelta(() => applyReplayAction("escape", surface)), 0, "Esc moves nothing");
/* Neither do the pane keys. The Logs pane reads this count to choose a scroll behaviour, so a
   rail walk that armed it would make the next click jump instead of glide. */
for (const action of ["railFocusNext", "railMoveNext", "railToggleBleed"]) {
  assert.equal(seekDelta(() => applyReplayAction(action, surface)), 0, `${action} moves no playhead`);
}
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

/* The scrubber's own arrow keys are the one keyboard seek that does not come through
   `applyReplayAction`. `resolveReplayAction` declines the navigation keys while a range input is
   focused — deliberately, so the native control keeps stepping itself — which means that movement
   reaches the run through `onScrub`, and before this it was the only key press the Logs pane
   scrolled smoothly for. StepController reports it through `noteKeyboardSeek` instead.

   So `noteKeyboardSeek` is asserted to be the same counter and the same rule, not a second one
   beside it: a reported move is worth exactly what an arrow key is worth, and a reported non-move
   is worth exactly what `→` at the live edge is worth. Change the guard inside it to count
   unconditionally and the fourth assertion below fails; delete the increment and the first two
   fail, along with every `applyReplayAction` assertion above, because that is the same rule. */
run.goTo(markerMid);
const arrowKeyWorth = seekDelta(() => applyReplayAction("stepForward", surface));
assert.equal(
  seekDelta(() => noteKeyboardSeek(markerMid, markerMid + 1)),
  arrowKeyWorth,
  "a scrubber arrow that moved the playhead must count for what a window arrow counts for"
);
assert.equal(
  seekDelta(() => noteKeyboardSeek(0, total)),
  arrowKeyWorth,
  "how far the scrubber jumped is not the question — PageUp and a drag cover the same distance"
);
/* A range fires no `input` when an arrow cannot move it, but Home at index 0 and End at the live
   edge do fire one, carrying the value the scrubber already had. Those must leave no mark, for the
   same reason the window's `→` at the live edge must not: the next drag would inherit it. */
assert.equal(
  seekDelta(() => noteKeyboardSeek(0, 0)),
  0,
  "Home on a scrubber already at the first event moves nothing and must not arm the next drag"
);
assert.equal(
  seekDelta(() => noteKeyboardSeek(total, total)),
  0,
  "End on a scrubber already at the live edge moves nothing and must not arm the next drag"
);
console.log("  scrubber arrow keys count through the same rule as the window bindings");

/* What the count is *for*: choosing a scroll behaviour. `"auto"` is not a way to spell instant —
   it defers to the container's `scroll-behavior`, which is unset everywhere in this tree today, so
   it reads as instant by luck and would turn smooth again the day any stylesheet sets it. Both
   scroll callers here have a branch that must never animate — one because a key press should land
   like a caret, one because the reader asked for reduced motion — and `"instant"` is the only value
   that ignores CSS. Measured, not assumed: ui/tools/scroll-probe/probe.mjs forces
   `scroll-behavior: smooth` on the real scroller and watches `"auto"` animate over ~113 frames
   while `"instant"` lands in one.

   Swept over the whole tree rather than pinned to the two known callers, because the failure mode
   is a third caller written later that copies the wrong word from a sibling. */
const scrollBehaviours = [];
for (const file of await fs.readdir(new URL("../src", import.meta.url), {
  recursive: true,
  withFileTypes: true
})) {
  if (!file.isFile() || !/\.(svelte|ts)$/.test(file.name)) continue;
  const path = `${file.parentPath}/${file.name}`;
  const source = await fs.readFile(path, "utf8");
  if (!/\.(scrollIntoView|scrollTo|scrollBy)\(/.test(source)) continue;
  /* The value is taken as the whole rest of the line and then scanned for every keyword in it,
     rather than the first one: both callers spell it as a ternary, so a `"smooth" : "auto"` would
     hide from a pattern that stopped at the first quoted word. */
  for (const [, expression] of source.matchAll(/behavior:\s*([^\n]*)/g)) {
    for (const [, quoted] of expression.matchAll(/["'](auto|instant|smooth)["']/g)) {
      scrollBehaviours.push({ file: path.replace(/.*\/src\//, "src/"), value: quoted });
    }
  }
}
assert.ok(
  scrollBehaviours.length >= 2,
  `expected to find the scroll callers' behaviours and found ${scrollBehaviours.length} — the ` +
    "sweep above has gone stale, so it is guarding nothing"
);
for (const { file, value } of scrollBehaviours) {
  assert.notEqual(
    value,
    "auto",
    `${file} passes behavior: "auto" to a scroll call. "auto" asks the container's CSS, which is ` +
      'the one thing a "do not animate this" branch cannot depend on: nothing sets ' +
      "`scroll-behavior` in ui/ today, so it happens to be instant, and the first stylesheet to " +
      'set `scroll-behavior: smooth` turns it into an animation. Say "instant", which never ' +
      "consults CSS."
  );
}
console.log(
  `  ${scrollBehaviours.length} scroll behaviours across ${new Set(scrollBehaviours.map((b) => b.file)).size} ` +
    'files, none of them "auto"'
);

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
