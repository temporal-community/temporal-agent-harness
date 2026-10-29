// ABOUTME: Asserts that a stacked (split) column moves along the rail as one column, by drag and
// by keyboard, and survives the address-bar round trip — while a lone pane still moves alone and a
// pane dropped on its own column's edge still detaches — and holds its width as focus moves inside it.

import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it, vi } from "vitest";

import { PANE_META } from "../panes/registry.ts";
import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { readStoredDesk } from "./agentRunStorage.ts";
import { createPaneStack, dockFocusedPane, openDefaultDrawer } from "./paneStack.svelte.ts";

function stubWindow() {
  const location = { pathname: "/", search: "" };
  globalThis.window = {
    location,
    history: {
      replaceState(_state, _title, url) {
        location.search = url.slice(url.indexOf("?") === -1 ? url.length : url.indexOf("?"));
      }
    },
    setTimeout,
    clearTimeout
  };
}

const ids = (stack) => stack.groups.map((group) => group.map((pane) => pane.id));

/* graph | usage+decisions (stacked) | chat | logs */
function deskWithStack() {
  const stack = createPaneStack();
  stack.openPane({ kind: "logs" });
  stack.openPane({ kind: "usage" });
  stack.openPane({ kind: "decisions" });
  stack.placePane("usage", "chat", "before");
  stack.placePane("decisions", "usage", "below");
  const [tokens, approvals] = stack.groups[1];
  tokens.share = 240;
  tokens.pinned = true;
  assert.deepEqual(ids(stack), [["graph"], ["usage", "decisions"], ["chat"], ["logs"]]);
  return { stack, tokens, approvals };
}

describe("moving a stacked column", () => {
  beforeEach(stubWindow);

  it("drags as one column, keeping order, split, share and pin", () => {
    const { stack } = deskWithStack();
    stack.placePane("decisions", "logs", "after");
    assert.deepEqual(ids(stack), [["graph"], ["chat"], ["logs"], ["usage", "decisions"]]);
    const [tokens, approvals] = stack.groups[3];
    assert.ok(tokens.split && approvals.split, "still stacked, not tabbed");
    assert.equal(tokens.share, 240);
    assert.equal(tokens.pinned, true);

    stack.writeQuery(0);
    const reloaded = createPaneStack();
    reloaded.hydrateFromQuery();
    assert.deepEqual(ids(reloaded), ids(stack));
    assert.ok(reloaded.groups[3].every((pane) => pane.split), "the link reopens it stacked");
  });

  it("moves by keyboard as one column", () => {
    const { stack } = deskWithStack();
    stack.movePane("usage", -1);
    assert.deepEqual(ids(stack), [["usage", "decisions"], ["graph"], ["chat"], ["logs"]]);
    stack.movePane("decisions", 2);
    assert.deepEqual(ids(stack), [["graph"], ["chat"], ["usage", "decisions"], ["logs"]]);
  });

  it("still detaches one pane dropped on its own column's edge, or into another column", () => {
    const { stack } = deskWithStack();
    stack.placePane("decisions", "usage", "after");
    assert.deepEqual(ids(stack), [["graph"], ["usage"], ["decisions"], ["chat"], ["logs"]]);

    const again = deskWithStack().stack;
    again.placePane("decisions", "logs", "tab");
    assert.deepEqual(ids(again), [["graph"], ["usage"], ["chat"], ["logs", "decisions"]]);
  });

  it("leaves single-pane and tabbed moves as they were", () => {
    const { stack } = deskWithStack();
    stack.placePane("logs", "graph", "before");
    assert.deepEqual(ids(stack), [["logs"], ["graph"], ["usage", "decisions"], ["chat"]]);

    stack.setSplit("usage", false);
    stack.placePane("decisions", "logs", "before");
    assert.deepEqual(ids(stack), [["decisions"], ["logs"], ["graph"], ["usage"], ["chat"]]);
  });

  /* Focus lands on pointerdown, so a width that followed it moved a header's
     buttons out from under the press before the release: one click to focus the
     pane, a second to hit the collapse button. */
  it("holds its width while focus moves between its panes", () => {
    const { stack } = deskWithStack();
    const column = () => stack.groups[1];
    stack.focusPane("usage");
    const width = stack.sizeOfGroup(column());
    stack.focusPane("decisions");
    assert.equal(stack.sizeOfGroup(column()), width);
    assert.equal(width, 440, "the widest pane's default, so neither is squeezed");

    stack.setSplit("usage", false);
    stack.focusPane("decisions");
    assert.equal(stack.sizeOfGroup(column()), 440, "tabbed, the front tab still decides");
    stack.focusPane("usage");
    assert.equal(stack.sizeOfGroup(column()), 400);
  });
});

describe("moving a pane between rails", () => {
  beforeEach(stubWindow);

  it("uses the same landing rules in both directions and writes p and p2", () => {
    const main = createPaneStack();
    const drawer = createPaneStack({ queryPrefix: "2", initial: [] });
    main.openPane({ kind: "logs" });
    drawer.openPane({ kind: "latency" });

    main.transferPane("logs", drawer, "latency", "tab");
    assert.deepEqual(ids(main), [["graph"], ["chat"]]);
    assert.deepEqual(ids(drawer), [["latency", "logs"]]);

    drawer.transferPane("logs", main, "graph", "after");
    assert.deepEqual(ids(main), [["graph"], ["logs"], ["chat"]]);
    assert.deepEqual(ids(drawer), [["latency"]]);

    main.writeQuery(0);
    drawer.writeQuery(0);
    const params = new URLSearchParams(window.location.search);
    assert.equal(params.get("p"), "graph,logs,chat");
    assert.equal(params.get("p2"), "latency");
  });

  it("round-trips an empty main rail after its last pane moves to the drawer", () => {
    const main = createPaneStack();
    const drawer = createPaneStack({ queryPrefix: "2", initial: [] });
    drawer.openPane({ kind: "latency" });

    main.transferPane("graph", drawer, "latency", "tab");
    main.transferPane("chat", drawer, "graph", "tab");
    main.writeQuery(0);
    drawer.writeQuery(0);
    assert.equal(new URLSearchParams(window.location.search).get("p"), "");

    const reloadedMain = createPaneStack();
    const reloadedDrawer = createPaneStack({ queryPrefix: "2", initial: [] });
    reloadedMain.hydrateFromQuery();
    reloadedDrawer.hydrateFromQuery();
    assert.deepEqual(ids(reloadedMain), []);
    assert.deepEqual(ids(reloadedDrawer), [["latency", "graph", "chat"]]);
  });
});

describe("a single-pane drawer", () => {
  beforeEach(stubWindow);

  /* graph | chat | logs on the rail, the timeline folded in the drawer. */
  function desk() {
    const main = createPaneStack();
    const drawer = createPaneStack({ queryPrefix: "2", initial: [], single: true });
    main.openPane({ kind: "logs" });
    drawer.openPane({ kind: "timeline" });
    drawer.toggleCollapse("timeline");
    return { main, drawer };
  }

  it("evicts its occupant to the rail, open, wherever the arrival was dropped", () => {
    for (const edge of ["tab", "before", "after", "above", "below"]) {
      const { main, drawer } = desk();
      main.transferPane("chat", drawer, "timeline", edge);
      assert.deepEqual(ids(drawer), [["chat"]], edge);
      assert.deepEqual(ids(main), [["graph"], ["timeline"], ["logs"]], edge);
      assert.equal(main.panes.find((pane) => pane.id === "timeline").collapsed, false);
    }
  });

  it("gives the evicted pane a column of its own rather than an occupied one", () => {
    const { main, drawer } = desk();
    main.placePane("logs", "chat", "tab");
    main.transferPane("logs", drawer, "timeline", "tab");
    assert.deepEqual(ids(main), [["graph"], ["chat"], ["timeline"]]);

    main.transferPane("graph", drawer, "logs", "tab");
    assert.deepEqual(ids(main), [["logs"], ["chat"], ["timeline"]]);
    assert.deepEqual(ids(drawer), [["graph"]]);
  });

  it("comes back from a reload as the last swap left it", () => {
    const { main, drawer } = desk();
    main.transferPane("chat", drawer, "timeline", "tab");
    main.transferPane("graph", drawer, "chat", "tab");
    main.writeQuery(0);
    drawer.writeQuery(0);

    const reloadedMain = createPaneStack();
    const reloadedDrawer = createPaneStack({ queryPrefix: "2", initial: [], single: true });
    reloadedMain.hydrateFromQuery();
    reloadedDrawer.hydrateFromQuery();
    assert.deepEqual(ids(reloadedDrawer), [["graph"]]);
    assert.deepEqual(ids(reloadedMain), [["chat"], ["timeline"], ["logs"]]);
    assert.ok(reloadedMain.panes.every((pane) => !pane.collapsed), "the evicted pane stays unfolded");
  });

  it("does nothing when its own occupant is dropped back on it", () => {
    const { main, drawer } = desk();
    drawer.transferPane("timeline", drawer, "timeline", "tab");
    main.transferPane("timeline", drawer, "timeline", "tab");
    assert.deepEqual(ids(drawer), [["timeline"]]);
    assert.deepEqual(ids(main), [["graph"], ["chat"], ["logs"]]);
  });
});

describe("dockFocusedPane — the keyboard route into and out of the drawer", () => {
  beforeEach(stubWindow);

  function desk() {
    const main = createPaneStack();
    const drawer = createPaneStack({ queryPrefix: "2", initial: [], single: true });
    main.openPane({ kind: "logs" });
    drawer.openPane({ kind: "timeline" });
    return { main, drawer };
  }

  it("sends the focused rail pane to an occupied drawer, evicting its occupant open and focused", () => {
    const { main, drawer } = desk();
    main.focusPane("chat");
    dockFocusedPane(main, drawer);
    assert.deepEqual(ids(drawer), [["chat"]]);
    assert.deepEqual(ids(main), [["graph"], ["timeline"], ["logs"]]);
    assert.equal(main.panes.find((pane) => pane.id === "timeline").collapsed, false);
    assert.equal(main.focusedId, "timeline", "the evicted pane keeps focus where it landed");
    assert.equal(drawer.focusedId, "chat");
  });

  it("sends the drawer's own occupant back to the rail, which is what makes the key a toggle", () => {
    const { main, drawer } = desk();
    drawer.focusPane("timeline");
    dockFocusedPane(drawer, main);
    assert.deepEqual(ids(drawer), []);
    assert.deepEqual(ids(main), [["graph"], ["chat"], ["logs"], ["timeline"]]);
    assert.equal(main.focusedId, "timeline");
  });

  it("lands in an empty drawer alone, focused, rather than needing an occupant to trade with", () => {
    const main = createPaneStack();
    const drawer = createPaneStack({ queryPrefix: "2", initial: [], single: true });
    main.focusPane("chat");
    dockFocusedPane(main, drawer);
    assert.deepEqual(ids(drawer), [["chat"]]);
    assert.deepEqual(ids(main), [["graph"]]);
    assert.equal(drawer.focusedId, "chat", "the arrival is focused, not left with no focus at all");
  });

  it("does nothing when the source stack has no focused pane", () => {
    const { main, drawer } = desk();
    main.focusedId = null;
    dockFocusedPane(main, drawer);
    assert.deepEqual(ids(main), [["graph"], ["chat"], ["logs"]]);
    assert.deepEqual(ids(drawer), [["timeline"]]);
  });
});

/* A scrub asks for a URL write per pointer move, and Chrome drops same-document navigations past
   its rate limit. Writes are held to one per 250ms: at once after a quiet spell, and once more at
   the end of a burst carrying where it ended — rebuilt then, so a pane opened meanwhile is in it. */
describe("throttled address-bar writes", () => {
  afterEach(() => vi.useRealTimers());

  it("writes leading and trailing, rebuilt at the trailing edge, and skips an unchanged URL", () => {
    vi.useFakeTimers({ now: 1_000_000 });
    stubWindow();
    const writes = [];
    const replace = window.history.replaceState;
    window.history.replaceState = (state, title, url) => (writes.push(url), replace(state, title, url));
    const cursor = () => new URLSearchParams(window.location.search).get("i");
    const stack = createPaneStack();

    stack.writeQuery(1);
    assert.equal(writes.length, 1, "the first write lands at once");
    assert.equal(cursor(), "1");

    vi.advanceTimersByTime(50);
    stack.writeQuery(2);
    stack.writeQuery(3);
    vi.advanceTimersByTime(199);
    assert.equal(writes.length, 1, "a burst inside the window waits");
    vi.advanceTimersByTime(1);
    assert.equal(writes.length, 2, "then lands once, 250ms after the last write");
    assert.equal(cursor(), "3", "carrying where the burst ended");

    vi.advanceTimersByTime(10);
    stack.writeQuery(4);
    stack.openPane({ kind: "usage" });
    vi.advanceTimersByTime(250);
    assert.equal(writes.length, 3);
    assert.equal(cursor(), "4");
    assert.match(window.location.search, /usage/, "rebuilt against the desk as it is when it lands");

    vi.advanceTimersByTime(1_000);
    stack.writeQuery(4);
    assert.equal(writes.length, 3, "an unchanged URL is not written");
    stack.writeQuery(5);
    assert.equal(writes.length, 4, "after a quiet spell, at once again");
    assert.equal(cursor(), "5");
    vi.advanceTimersByTime(1_000);
    assert.equal(writes.length, 4, "and nothing trails a lone write");
  });
});

describe("pane widths across a reload", () => {
  const nativeRaf = globalThis.requestAnimationFrame;
  const nativeCancel = globalThis.cancelAnimationFrame;

  afterEach(() => {
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.sessionStorage;
    globalThis.requestAnimationFrame = nativeRaf;
    globalThis.cancelAnimationFrame = nativeCancel;
  });

  it("keeps dragged widths when the rail measures 0 before it has a size", () => {
    installBrowserSurface();
    const sessionId = "wf-widths";
    const stack = createPaneStack();
    stack.setGroupSize("graph", 880);
    stack.setGroupSize("chat", 640);
    stack.enterSession(sessionId);

    const reloaded = createPaneStack();
    reloaded.hydrateFromQuery();
    reloaded.enterSession(sessionId);
    const column = (id) => reloaded.groups.find((group) => group.some((pane) => pane.id === id));

    reloaded.noteRailWidth(0);
    reloaded.persistActiveDesk();
    assert.equal(column("graph")[0].size, 880);
    assert.equal(column("chat")[0].size, 640);

    reloaded.noteRailWidth(1100);
    reloaded.persistActiveDesk();
    assert.equal(reloaded.sizeOfGroup(column("graph")), 880, "a narrow rail scrolls, it does not scale");
    assert.equal(reloaded.sizeOfGroup(column("chat")), 640);

    const stored = readStoredDesk(sessionId);
    const storedSize = (id) => stored.flat().find((pane) => pane.id === id)?.size;
    assert.equal(storedSize("graph"), 880);
    assert.equal(storedSize("chat"), 640);
    assert.notEqual(storedSize("graph"), PANE_META.graph.minSize);
    assert.notEqual(storedSize("chat"), PANE_META.chat.minSize);
  });
});

/* The drawer lives in the link (`p2`, folded as `c2`) and the rail in the link and the session's
   desk. A swap writes both, and a shut drawer is folded rather than emptied, so neither a reload
   nor shutting and reopening it loses the pane that was swapped in. */
describe("a drawer swap across shutting and reloading", () => {
  const SESSION = "wf-drawer-swap";

  beforeEach(() => {
    vi.useFakeTimers({ now: 1_000_000 });
    installBrowserSurface();
    const location = window.location;
    window.history.replaceState = (_state, _title, url) => {
      location.search = url.slice(url.indexOf("?") === -1 ? url.length : url.indexOf("?"));
    };
  });

  afterEach(() => {
    vi.useRealTimers();
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.sessionStorage;
  });

  /* What App.svelte does on load, in its order. */
  function boot() {
    const rail = createPaneStack();
    rail.hydrateFromQuery();
    const drawer = createPaneStack({ queryPrefix: "2", initial: [], single: true, peer: rail });
    drawer.hydrateFromQuery();
    openDefaultDrawer(drawer, window.location.search);
    rail.enterSession(SESSION);
    return { rail, drawer };
  }

  function persist({ rail, drawer }) {
    rail.writeQuery(0);
    drawer.writeQuery(0);
    rail.persistActiveDesk();
    vi.advanceTimersByTime(1_000);
  }

  const query = (key) => new URLSearchParams(window.location.search).get(key);
  const deskIds = () => readStoredDesk(SESSION).map((group) => group.map((pane) => pane.id));
  const everyId = ({ rail, drawer }) => [...rail.panes, ...drawer.panes].map((pane) => pane.id);

  function swapped() {
    const desk = boot();
    assert.deepEqual(ids(desk.drawer), [["timeline"]], "a fresh visit opens on the default");
    desk.rail.transferPane("chat", desk.drawer, "timeline", "tab");
    persist(desk);
    return desk;
  }

  it("stores the arrival in the drawer and its occupant on the rail", () => {
    swapped();
    assert.equal(query("p2"), "chat");
    assert.equal(query("p"), "graph,timeline");
    assert.deepEqual(deskIds(), [["graph"], ["timeline"]]);
  });

  it("comes back from a reload exactly as swapped, each pane once, with no default added", () => {
    swapped();
    const reloaded = boot();
    assert.deepEqual(ids(reloaded.drawer), [["chat"]]);
    assert.deepEqual(ids(reloaded.rail), [["graph"], ["timeline"]]);
    const all = everyId(reloaded);
    assert.equal(new Set(all).size, all.length, "no pane is open twice");

    reloaded.rail.enterSession("wf-other");
    reloaded.rail.enterSession(SESSION);
    assert.deepEqual(ids(reloaded.drawer), [["chat"]], "the drawer is global, not per session");
    assert.deepEqual(ids(reloaded.rail), [["graph"], ["timeline"]]);
  });

  it("keeps the arrival through shutting, reloading shut, and reopening", () => {
    const desk = swapped();
    desk.drawer.foldAll(true);
    persist(desk);
    assert.equal(query("p2"), "chat", "shut is not emptied");
    assert.equal(query("c2"), "chat");

    const reloaded = boot();
    assert.ok(reloaded.drawer.allFolded, "it reloads shut");
    assert.deepEqual(ids(reloaded.drawer), [["chat"]]);
    assert.deepEqual(ids(reloaded.rail), [["graph"], ["timeline"]]);

    reloaded.drawer.foldAll(false);
    persist(reloaded);
    assert.deepEqual(ids(reloaded.drawer), [["chat"]]);
    assert.equal(reloaded.drawer.allFolded, false);
    assert.equal(query("c2"), null);
  });
});
