// ABOUTME: Asserts that a stacked (split) column moves along the rail as one column, by drag and
// by keyboard, and survives the address-bar round trip — while a lone pane still moves alone and a
// pane dropped on its own column's edge still detaches — and holds its width as focus moves inside it.

import assert from "node:assert/strict";
import { beforeEach, describe, it } from "vitest";

import { createPaneStack } from "./paneStack.svelte.ts";

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
    assert.equal(width, 660, "the widest pane's default, so neither is squeezed");

    stack.setSplit("usage", false);
    stack.focusPane("decisions");
    assert.equal(stack.sizeOfGroup(column()), 660, "tabbed, the front tab still decides");
    stack.focusPane("usage");
    assert.equal(stack.sizeOfGroup(column()), 400);
  });
});
