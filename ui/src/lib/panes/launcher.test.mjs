// ABOUTME: Asserts the pane launcher is a list of switches that stays open: each row says whether
// its view is open anywhere (rail or bottom drawer), a press opens or closes it without shutting
// the menu, a pinned view is marked aria-disabled rather than ignoring the press, the arrow/Home/
// End/Enter/Space map follows the WAI-ARIA menu pattern, and Escape, an outside press and the `+`
// shut it with focus back on the `+`. No DOM package: the decisions live in launcher.ts and are
// driven directly, the dismiss layer is driven through a stub DOM, and the wiring that needs a
// browser is checked in PaneMinimap's source.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { createPaneStack } from "../state/paneStack.svelte.ts";
import { dismissable } from "../state/dismissable.svelte.ts";
import { PINNED_REASON, handleLauncherKey, launcherItems, toggleView } from "./launcher.ts";

const minimap = readFileSync(new URL("./PaneMinimap.svelte", import.meta.url), "utf8");

const desk = () => ({ rail: createPaneStack(), drawer: createPaneStack({ queryPrefix: "2", initial: [] }) });
const openKinds = (stacks) => launcherItems(stacks).filter((item) => item.open).map((item) => item.kind);
const ids = (stack) => stack.panes.map((pane) => pane.id);

describe("launcher rows", () => {
  it("list every view, and mark the open ones", () => {
    const { rail, drawer } = desk();
    const items = launcherItems([rail, drawer]);
    assert.deepEqual(
      items.map((item) => item.kind),
      ["chat", "graph", "decisions", "logs", "latency", "usage", "state", "files"],
      "an open view must stay in the list so it can be switched off"
    );
    assert.deepEqual(openKinds([rail, drawer]), ["chat", "graph"]);
  });

  it("count a view held only by the drawer as open", () => {
    const { rail, drawer } = desk();
    drawer.openPane({ kind: "latency" });
    assert.ok(launcherItems([rail, drawer]).find((item) => item.kind === "latency").open);
  });

  it("do not count a scoped drill-in as the launcher's view", () => {
    const { rail, drawer } = desk();
    rail.openPane({ kind: "logs", key: "wf-1" });
    assert.ok(!launcherItems([rail, drawer]).find((item) => item.kind === "logs").open);
  });
});

describe("toggling a view", () => {
  it("opens a closed view beside the focused pane, and repeated opens chain rightward", () => {
    const { rail, drawer } = desk();
    rail.focusPane("graph");
    toggleView("logs", rail, [rail, drawer]);
    toggleView("usage", rail, [rail, drawer]);
    assert.deepEqual(ids(rail), ["graph", "logs", "usage", "chat"]);
    assert.equal(rail.focusedId, "usage");
  });

  it("closes an open view, and closes it in the drawer when that is where it is", () => {
    const { rail, drawer } = desk();
    toggleView("chat", rail, [rail, drawer]);
    assert.deepEqual(ids(rail), ["graph"]);

    drawer.openPane({ kind: "latency" });
    toggleView("latency", rail, [rail, drawer]);
    assert.deepEqual(ids(drawer), [], "closed where it was");
    assert.ok(!rail.has("latency"), "and not opened on the rail instead");
  });

  it("marks a pinned view locked and leaves it open when pressed", () => {
    const { rail, drawer } = desk();
    rail.togglePin("chat");
    const chat = launcherItems([rail, drawer]).find((item) => item.kind === "chat");
    assert.equal(chat.locked, PINNED_REASON);
    toggleView("chat", rail, [rail, drawer]);
    assert.ok(rail.has("chat"));
    assert.equal(rail.canClose("chat"), false, "the same guard the pane header's close obeys");
  });

  it("does not half-close a view pinned in one stack and loose in the other", () => {
    const { rail, drawer } = desk();
    drawer.openPane({ kind: "chat" });
    drawer.togglePin("chat");
    toggleView("chat", rail, [rail, drawer]);
    assert.ok(rail.has("chat") && drawer.has("chat"));
  });
});

describe("launcher keys", () => {
  const press = (key, index, count = 7, mods = {}) => {
    const log = [];
    let prevented = false;
    handleLauncherKey(
      { key, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...mods, preventDefault: () => (prevented = true) },
      index,
      count,
      { move: (i) => log.push(["move", i]), toggle: (i) => log.push(["toggle", i]) }
    );
    return { log, prevented };
  };

  it("walks with the arrows, wrapping at both ends", () => {
    assert.deepEqual(press("ArrowDown", 2).log, [["move", 3]]);
    assert.deepEqual(press("ArrowDown", 6).log, [["move", 0]]);
    assert.deepEqual(press("ArrowUp", 0).log, [["move", 6]]);
  });

  it("jumps with Home and End, and claims them from the replay keys", () => {
    const home = press("Home", 4);
    assert.deepEqual(home.log, [["move", 0]]);
    assert.ok(home.prevented, "Home would otherwise also seek the replay to its first event");
    assert.deepEqual(press("End", 1).log, [["move", 6]]);
  });

  it("toggles the focused row on Enter and Space", () => {
    const enter = press("Enter", 3);
    assert.deepEqual(enter.log, [["toggle", 3]]);
    assert.ok(enter.prevented, "or the button's own click would toggle it back");
    assert.deepEqual(press(" ", 5).log, [["toggle", 5]]);
  });

  it("leaves modified and unrelated keys alone", () => {
    assert.deepEqual(press("ArrowDown", 0, 7, { metaKey: true }), { log: [], prevented: false });
    assert.deepEqual(press("Tab", 0), { log: [], prevented: false });
    assert.deepEqual(press("Escape", 0), { log: [], prevented: false }, "dismissable owns Escape");
  });
});

describe("the menu in PaneMinimap", () => {
  it("renders each view as a checkbox item with its open state", () => {
    assert.match(minimap, /role="menuitemcheckbox"/);
    assert.match(minimap, /aria-checked=\{view\.open\}/);
    assert.match(minimap, /aria-disabled=\{view\.locked \? "true" : undefined\}/);
    assert.match(minimap, /data-tip=\{view\.locked \?\? undefined\}/, "a locked row says why on hover");
    assert.doesNotMatch(minimap, /role="menuitem"[\s>]/);
  });

  it("stays open after a toggle: only the `+` and the dismiss layer shut it", () => {
    const writes = [...minimap.matchAll(/\blauncherOpen = (!?[\w$]+)/g)].map((match) => match[1]);
    assert.deepEqual(writes, ["$state", "!launcherOpen", "false"]);
    assert.match(minimap, /ondismiss: \(\) => \(launcherOpen = false\), keep: "\.launcher"/);
  });

  it("puts focus on the `+` before opening, so dismissing hands focus back to it", () => {
    assert.match(minimap, /if \(!launcherOpen\) \{\s*\(event\.currentTarget as HTMLElement\)\.focus/);
    /* Attachment order is mount order: the opener must be recorded before focus moves in. */
    assert.ok(minimap.indexOf("{@attach dismissable(") < minimap.indexOf("{@attach focusOnOpen}"));
  });

  it("sizes to its longest label on one line instead of wrapping it", () => {
    const menuRule = minimap.match(/\n  \.launch-menu \{([^}]*)\}/)[1];
    assert.match(menuRule, /width: max-content;/);
    assert.match(menuRule, /right: 0;/, "grows leftward from the launcher");
    assert.match(menuRule, /max-width: calc\(100vw/, "capped by the viewport");
    assert.doesNotMatch(menuRule, /(^|[^-])width: \d+px/, "a fixed width wraps APPROVAL DECISIONS");
    assert.match(minimap, /\.launch-menu \.kicker \{[^}]*white-space: nowrap;/);
  });

  it("sizes to its longest label on one line instead of wrapping it", () => {
    const menuRule = minimap.match(/\n  \.launch-menu \{([^}]*)\}/)[1];
    assert.match(menuRule, /width: max-content;/);
    assert.match(menuRule, /right: 0;/, "grows leftward from the launcher");
    assert.match(menuRule, /max-width: calc\(100vw/, "capped by the viewport");
    assert.doesNotMatch(menuRule, /(^|[^-])width: \d+px/, "a fixed width wraps APPROVAL DECISIONS");
    assert.match(minimap, /\.launch-menu \.kicker \{[^}]*white-space: nowrap;/);
  });

  it("uses roving focus: one row in the tab order", () => {
    assert.match(minimap, /tabindex=\{index === activeView \? 0 : -1\}/);
  });
});

/* --- dismissing, through the same stub DOM dismissable.test.mjs uses ------ */
class FakeNode {
  constructor(parent = null, cls = null) {
    this.parent = parent;
    this.cls = cls;
    this.isConnected = true;
  }
  contains(other) {
    for (let n = other; n; n = n.parent) if (n === this) return true;
    return false;
  }
  closest(selector) {
    for (let n = this; n; n = n.parent) if (`.${n.cls}` === selector) return n;
    return null;
  }
  focus() {
    globalThis.document.activeElement = this;
  }
}

describe("closing the launcher", () => {
  const listeners = new Map();
  globalThis.Node = FakeNode;
  globalThis.Element = FakeNode;
  globalThis.HTMLElement = FakeNode;
  globalThis.window ??= {};
  globalThis.window.addEventListener = (type, fn, capture) => listeners.set(`${type}:${capture}`, fn);
  globalThis.window.removeEventListener = (type, _fn, capture) => listeners.delete(`${type}:${capture}`);
  const body = new FakeNode();
  globalThis.document = { body, activeElement: body };

  const launcher = new FakeNode(body, "launcher");
  const plus = new FakeNode(launcher);
  const elsewhere = new FakeNode(body);

  /* Mirrors the component: focus the `+`, mount the menu with the launcher's options, move focus in. */
  function open() {
    const state = { open: true };
    plus.focus();
    const menu = new FakeNode(launcher);
    const item = new FakeNode(menu);
    const teardown = dismissable({ ondismiss: () => (state.open = false), keep: ".launcher" })(menu);
    item.focus();
    return { state, close: () => teardown() };
  }
  const fire = (type, event) => listeners.get(`${type}:true`)?.(event);

  it("Escape shuts it and focus returns to the `+`", () => {
    const { state, close } = open();
    fire("keydown", { key: "Escape", stopPropagation() {} });
    assert.equal(state.open, false);
    close();
    assert.equal(document.activeElement, plus);
  });

  it("an outside press shuts it and focus returns to the `+`", () => {
    const { state, close } = open();
    fire("pointerdown", { target: elsewhere });
    assert.equal(state.open, false);
    close();
    assert.equal(document.activeElement, plus);
  });

  it("a press on the `+` is left to the `+`'s own toggle, and focus returns to it", () => {
    const { state, close } = open();
    fire("pointerdown", { target: plus });
    assert.equal(state.open, true, "dismissing here too would shut and reopen it on one press");
    close();
    assert.equal(document.activeElement, plus);
  });
});
