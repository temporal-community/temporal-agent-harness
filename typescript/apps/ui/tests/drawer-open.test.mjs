// ABOUTME: Asserts that a pane in the bottom drawer does not play the rail's slide-in entrance.
// The entrance moves a new pane in from the right; the drawer rises from below, so opening it
// swept the latency waterfall sideways out of a box that was moving up.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");
const shell = readFileSync(new URL("../src/lib/panes/PaneShell.svelte", import.meta.url), "utf8");

describe("the bottom drawer opens in place", () => {
  it("turns off the pane entrance the shell still plays in the rail", () => {
    assert.match(shell, /@starting-style\s*\{\s*\.pane\s*\{[^}]*translateX/, "the rail's entrance this guards against has moved");
    assert.match(app, /\.drawer :global\(\.pane\) \{\s*transition: none;\s*\}/);
  });
});
