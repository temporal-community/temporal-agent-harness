import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, it } from "vitest";

import { createPaneStack } from "../src/lib/state/paneStack.svelte.ts";
import { installBrowserSurface } from "./support/controllerHarness.mjs";

const appSource = readFileSync(
  fileURLToPath(new URL("../src/App.svelte", import.meta.url)),
  "utf8"
);
const drawerSource = readFileSync(
  fileURLToPath(
    new URL("../src/lib/components/primitives/DockedDrawer.svelte", import.meta.url)
  ),
  "utf8"
);

describe("Session Manager drawer", () => {
  beforeEach(() => {
    installBrowserSurface();
  });

  it("treats the former sessions pane URL as unknown and keeps the default desk", () => {
    window.location.search = "?p=sessions";

    const stack = createPaneStack();
    stack.hydrateFromQuery();

    assert.deepEqual(stack.panes.map((pane) => pane.id), ["graph", "chat"]);
    assert.deepEqual(stack.unknownPanes, {
      tokens: [{ written: "sessions", meant: null }],
      fellBack: true
    });
  });

  it("uses the shared docked drawer on the left without duplicating the manager", () => {
    /* Two instances, one list: the launcher in the chrome names the session, and the
       manager it opens is the only thing that lists them. `session-launcher.test.mjs`
       holds the other half of this — that the launcher is still there at all. */
    assert.equal(appSource.match(/<SessionControls\b/g)?.length, 2);
    assert.equal(appSource.match(/display="pane"/g)?.length, 1);
    assert.equal(appSource.match(/<DockedDrawer\b/g)?.length, 2);
    assert.match(appSource, /<DockedDrawer\s+edge="left"/);
    assert.match(appSource, /<DockedDrawer\s+edge="bottom"/);
    assert.match(appSource, /aria-controls="session-manager-drawer"/);
    assert.match(appSource, /aria-expanded=\{sessionDrawerOpen\}/);
    assert.doesNotMatch(appSource, /@attach dismissable/);
    assert.match(
      appSource,
      /\.app\.has-session-drawer\s*\{[\s\S]*?grid-template-columns:/
    );
  });

  it("mirrors one resize implementation across bottom and left edges", () => {
    assert.match(drawerSource, /type DrawerEdge\n[^;]*from "\.\/resizeKeys"/);
    assert.match(drawerSource, /case "bottom":[\s\S]*?rect\.bottom - event\.clientY/);
    assert.match(drawerSource, /case "left":[\s\S]*?event\.clientX - rect\.left/);
    assert.match(drawerSource, /const _exhaustive: never = edge/);
  });
});
