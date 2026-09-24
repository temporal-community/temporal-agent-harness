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

  it("overlays the desk with one dismissable, focus-linked manager", () => {
    assert.match(appSource, /\.session-drawer\s*\{[\s\S]*?position:\s*absolute;/);
    assert.equal(appSource.match(/<SessionControls\b/g)?.length, 1);
    assert.match(appSource, /aria-controls="session-manager-drawer"/);
    assert.match(appSource, /aria-expanded=\{sessionManagerOpen\}/);
    assert.match(appSource, /@attach dismissable/);
  });
});
