// ABOUTME: The rail and the drawer share one drag state that each PaneRail writes. It has to be
// bound, not passed, or Svelte warns ownership_invalid_mutation on every drag.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const app = read("../../App.svelte");
const rail = read("./PaneRail.svelte");

describe("the shared pane drag state", () => {
  it("is a bindable prop that both rails in the app bind", () => {
    assert.match(rail, /dragState = \$bindable\(/);
    const passed = [...app.matchAll(/(\S*)dragState=\{paneDrag\}/g)].map(([, prefix]) => prefix);
    assert.deepEqual(passed, ["bind:", "bind:"], "the rail and the drawer, both bound");
  });
});
