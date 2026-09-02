/**
 * The replay hotkeys must stay quiet while the user is typing, and must stay
 * out of the way of controls that already handle the same keys. Both are pure
 * decisions, so they are checked here against the real table rather than a copy
 * of it — Node strips the types on import.
 */
import assert from "node:assert/strict";

const { REPLAY_BINDINGS, resolveReplayAction } = await import(
  new URL("../src/lib/state/replayHotkeys.ts", import.meta.url).href
);

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
assert.equal(resolveReplayAction(press({ key: "L" })), "jumpToLive", "letters match either case");
assert.equal(resolveReplayAction(press({ key: "?" })), "toggleHelp");
assert.equal(resolveReplayAction(press({ key: "q" })), null, "unbound keys do nothing");

// Nothing fires while the user is typing. This is the property that matters.
for (const key of ["ArrowLeft", "ArrowRight", "Home", "End", " ", "l", "?"]) {
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
assert.equal(resolveReplayAction(press({ key: "l", modified: true })), null);

// The focused scrubber keeps its free native stepping, with no second step.
for (const key of ["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"]) {
  assert.equal(
    resolveReplayAction(press({ key, rangeFocused: true })),
    null,
    `${key} on the focused scrubber must be left to the native range input`
  );
}
assert.equal(
  resolveReplayAction(press({ key: "l", rangeFocused: true })),
  "jumpToLive",
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

console.log(`replay hotkeys: ${REPLAY_BINDINGS.length} bindings, guards hold`);
