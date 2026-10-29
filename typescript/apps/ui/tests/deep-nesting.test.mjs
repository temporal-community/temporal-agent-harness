// ABOUTME: Pins the indent cap on the console's trees. A 30-level mount tree and a 30-level JSON
// state document stepped in once per level with no ceiling, so at 420px the name you click walked
// out of the pane. Past the cap a row stops stepping in and says its depth instead.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import AgentStatePanel from "../src/lib/components/agent/AgentStatePanel.svelte";
import MountTrees from "../src/lib/components/agent/MountTrees.svelte";
import { NEST_CAP } from "../src/lib/components/primitives/nest.ts";

const LEVELS = 30;

function deepTree() {
  let node = { path: "a/".repeat(LEVELS) + "leaf.txt", name: "leaf.txt", isDir: false, children: [] };
  for (let level = LEVELS; level > 0; level -= 1) {
    node = { path: "a/".repeat(level).slice(0, -1), name: "a", isDir: true, children: [node] };
  }
  const fill = (n) => ({ size: 1, mtime: 0, writes: 0, content: null, change: null, changedBelow: false, version: "1", ...n, children: n.children.map(fill) });
  return fill({ path: "", name: "", isDir: true, children: [node] });
}

function deepDoc() {
  let value = { leaf: true };
  for (let level = 0; level < LEVELS; level += 1) value = { a: value };
  return value;
}

describe("a deeply nested tree", () => {
  it("stops stepping a mount row in at the cap and labels its depth", () => {
    const mount = {
      key: "wf/mounts", kind: "tree", mount: "/deep", description: "", readOnly: false, label: "a",
      role: "parent", source: null, okfVersion: null, stateVersion: 1, root: deepTree(), fileCount: 1, problem: null
    };
    const body = render(MountTrees, { props: { mounts: [mount], selected: null, onOpen() {} } }).body;
    const depths = [...body.matchAll(/--depth: (\d+)/g)].map((match) => Number(match[1]));
    assert.equal(depths.length, LEVELS + 1);
    assert.equal(Math.max(...depths), NEST_CAP);
    assert.match(body, new RegExp(`class="nest-depth[^"]*">${LEVELS}<`));
  });

  it("stops indenting the JSON state document at the cap", () => {
    const doc = {
      key: "wf/plan", stateId: "plan", workflowId: "wf", label: "planner", role: "parent", version: 1,
      value: deepDoc(), changed: [], commits: 1, turnNumber: 1, problem: null
    };
    const body = render(AgentStatePanel, { props: { states: [doc] } }).body;
    const depths = [...body.matchAll(/class="line[^"]*"[^>]*style="--depth: (\d+);/g)].map((match) => Number(match[1]));
    assert.ok(depths.length > LEVELS, "the lines down to the deepest key are drawn");
    assert.equal(Math.max(...depths), NEST_CAP);
    assert.match(body, new RegExp(`class="nest-depth[^"]*">${LEVELS}<`));
  });
});

describe("the file viewer and OKF graph dialogs", () => {
  it("stack their two columns in a narrow container", () => {
    for (const file of ["FileViewerDialog", "OKFGraphDialog"]) {
      const css = readFileSync(new URL(`../src/lib/components/agent/${file}.svelte`, import.meta.url), "utf8");
      assert.match(css, /container: [a-z-]+ \/ inline-size;/, file);
      assert.match(css, /@container [a-z-]+ \(max-width: 40rem\) \{\s*\.columns \{\s*grid-template-columns: minmax\(0, 1fr\);/, file);
    }
  });
});
