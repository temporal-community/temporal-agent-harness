// ABOUTME: Pins the Session Manager as a quick switcher: ↑/↓ walk a highlight that clamps at both
// ends and survives the filter, Enter switches and closes, Escape closes without reaching the
// window's own Escape, focus goes back to where it came from, and `s` in the search box is a letter.
// There is no DOM here, so the keydown is driven through the shipped `handleListKey` with plain
// events, and the markup is read off a server render.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import SessionControls from "$lib/components/chat/SessionControls.svelte";
import { focusReturnTarget, handleListKey, keptHighlight } from "./quickSwitch.ts";
import { describeReplayKeyEvent, resolveReplayAction } from "./replayHotkeys.ts";

function key(name, overrides = {}) {
  const event = {
    key: name,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    isComposing: false,
    prevented: false,
    preventDefault() {
      event.prevented = true;
    },
    ...overrides
  };
  return event;
}

/** One press against a list of `count` rows with row `index` highlighted, recorded. */
function press(event, index, count) {
  const calls = [];
  handleListKey(event, index, count, {
    move: (to) => calls.push(["move", to]),
    pick: (at) => calls.push(["pick", at]),
    close: () => calls.push(["close"])
  });
  return { calls, prevented: event.prevented };
}

describe("the switcher's keys", () => {
  it("↑ and ↓ move the highlight and clamp at both ends", () => {
    assert.deepEqual(press(key("ArrowDown"), 0, 3).calls, [["move", 1]]);
    assert.deepEqual(press(key("ArrowDown"), 2, 3).calls, [["move", 2]], "↓ on the last row holds");
    assert.deepEqual(press(key("ArrowUp"), 1, 3).calls, [["move", 0]]);
    assert.deepEqual(press(key("ArrowUp"), 0, 3).calls, [["move", 0]], "↑ on the first row holds");
    assert.deepEqual(press(key("ArrowDown"), -1, 3).calls, [["move", 0]], "from no row, ↓ lands on the first");
    assert.equal(press(key("ArrowDown"), 0, 3).prevented, true, "the caret must not jump to the end of the field");
    assert.deepEqual(press(key("ArrowDown"), -1, 0).calls, [], "an empty list has nowhere to go");
  });

  it("Enter picks the highlighted row, and nothing without one", () => {
    assert.deepEqual(press(key("Enter"), 1, 3), { calls: [["pick", 1]], prevented: true });
    assert.deepEqual(press(key("Enter"), -1, 0).calls, []);
    assert.deepEqual(
      press(key("Enter", { isComposing: true }), 1, 3),
      { calls: [], prevented: false },
      "Enter mid-IME confirms the word, not the row"
    );
  });

  it("Escape closes, and is claimed so the window's Escape never sees it", () => {
    assert.deepEqual(press(key("Escape"), 0, 3), { calls: [["close"]], prevented: true });
    assert.deepEqual(press(key("Escape"), -1, 0).calls, [["close"]], "an empty filter still closes");
    const app = readFileSync(new URL("../../App.svelte", import.meta.url), "utf8");
    assert.match(
      app,
      /function handleWindowKeydown\(event: KeyboardEvent\): void \{\s*if \(event\.defaultPrevented\) return;/,
      "the window's handler must stand down on a claimed key, or Escape in the drawer also leaves full screen"
    );
  });

  it("leaves letters and modified keys to the field", () => {
    for (const event of [key("s"), key("ArrowDown", { shiftKey: true }), key("ArrowUp", { metaKey: true })]) {
      assert.deepEqual(press(event, 0, 3), { calls: [], prevented: false }, `${event.key} belongs to the field`);
    }
  });

  it("`s` in the search box types an s rather than toggling the drawer", () => {
    const search = { tagName: "INPUT", type: "", isContentEditable: false, getAttribute: () => null };
    const typed = (name) =>
      describeReplayKeyEvent(
        { key: name, shiftKey: false, altKey: false, ctrlKey: false, metaKey: false, target: search },
        { helpOpen: false, bleeding: true }
      );
    assert.equal(resolveReplayAction(typed("s")), null);
    assert.equal(resolveReplayAction(typed("Escape")), null, "nor does Escape there leave full screen");
  });
});

describe("the highlight and the filter", () => {
  it("stays on its row while the filter shows it, and falls to the first match when not", () => {
    assert.equal(keptHighlight(["a", "b", "c"], null), "a", "the first match by default");
    assert.equal(keptHighlight(["a", "b", "c"], "b"), "b");
    assert.equal(keptHighlight(["c", "d"], "b"), "c", "filtered away, so the first visible row");
    assert.equal(keptHighlight([], "b"), null, "no rows, no highlight");
  });

  it("renders as a combobox pointing at the first row", () => {
    const sessions = ["wf-new", "wf-mid", "wf-old"].map((workflow_id, i) => ({
      workflow_id,
      created_at: 300 - i * 100,
      agent_workflow_type: "Agent",
      initial_user_message: `message ${i}`
    }));
    const { body } = render(SessionControls, {
      props: { display: "pane", tab: "sessions", sessions, sessionId: "wf-mid" }
    });
    const input = body.match(/<input[^>]*>/)?.[0] ?? "";
    assert.match(input, /role="combobox"/);
    assert.match(input, /aria-controls="session-listbox"/);
    assert.match(input, /aria-expanded="true"/);
    assert.match(input, /aria-activedescendant="session-option-wf-new"/);
    assert.match(body, /id="session-listbox"[^>]*role="listbox"/);
    assert.doesNotMatch(body, /agent-glyph/, "the icon repeated the agent named on the row and the status chip beside it");
    const options = [...body.matchAll(/<button[^>]*role="option"[^>]*>/g)].map(([tag]) => tag);
    assert.equal(options.length, 3);
    assert.match(options[0], /id="session-option-wf-new"[^>]*aria-selected="true"/);
    assert.match(options[1], /aria-selected="false"/);
    assert.match(options[2], /aria-selected="false"/);
  });
});

describe("where focus goes back to", () => {
  const trigger = { name: "icon", isConnected: true };
  const drawer = { contains: (node) => node.inDrawer === true };

  it("the element focused before the drawer opened", () => {
    const pane = { name: "pane", isConnected: true };
    assert.equal(focusReturnTarget(pane, drawer, trigger), pane);
  });

  it("the icon, when that element is gone, was in the drawer, or there was none", () => {
    assert.equal(focusReturnTarget({ isConnected: false }, drawer, trigger), trigger);
    assert.equal(focusReturnTarget({ isConnected: true, inDrawer: true }, drawer, trigger), trigger);
    assert.equal(focusReturnTarget(null, drawer, trigger), trigger);
  });
});
