// ABOUTME: Asserts how tracked Code Mode mounts become trees: FileIndex and FileTree states are
// picked out of the agent states (and nothing else is), paths become nested nodes with their
// directories filled in, the commit at the cursor marks the paths it touched and every ancestor,
// and an in-memory tree carries its contents while an index carries only metadata and a source.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  buildMountViews,
  contentBytes,
  decodeText,
  fileView,
  findNode,
  isFileStateDoc
} from "./fileMounts.ts";

const doc = (stateId, value, changed = [], role = "parent") => ({
  key: `wf-1:${stateId}`,
  stateId,
  workflowId: "wf-1",
  label: role === "parent" ? "assistant" : "helper",
  role,
  version: 3,
  value,
  changed,
  commits: 3,
  turnNumber: 1,
  problem: null
});

const indexDoc = (changed = []) =>
  doc(
    "memory",
    {
      kind: "file_index",
      mount: "/memory",
      description: "Long-term memory.",
      read_only: false,
      source: { filesystem: "local-disk", config: { directory: "alice" }, task_queue: "q" },
      entries: {
        "MEMORY.md": { is_dir: false, size: 40, mtime: 12.5, writes: 1 },
        "topics/coffee.md": { is_dir: false, size: 11, mtime: null, writes: 2 },
        "listed-only": { is_dir: null, size: null, mtime: null, writes: 0 }
      }
    },
    changed
  );

const treeDoc = (changed = []) =>
  doc(
    "skills",
    {
      kind: "file_tree",
      mount: "/skills",
      description: "",
      read_only: true,
      directories: ["memory"],
      files: {
        "memory/SKILL.md": { content: "# Memory", encoding: "utf-8", size: 8 },
        "memory/logo.bin": { content: "AP8=", encoding: "base64", size: 2 }
      }
    },
    changed
  );

describe("buildMountViews", () => {
  it("picks out only the states that track mounts", () => {
    const plan = doc("plan", { steps: [] });
    assert.equal(isFileStateDoc(plan), false);
    assert.equal(isFileStateDoc(indexDoc()), true);
    const views = buildMountViews([plan, indexDoc(), treeDoc()]);
    assert.deepEqual(
      views.map((v) => [v.mount, v.kind, v.readOnly]),
      [
        ["/memory", "index", false],
        ["/skills", "tree", true]
      ]
    );
  });

  it("nests an index's paths, directories first, filling in parents", () => {
    const [view] = buildMountViews([indexDoc()]);
    assert.deepEqual(
      view.root.children.map((n) => [n.name, n.isDir]),
      [
        ["topics", true],
        ["listed-only", null],
        ["MEMORY.md", false]
      ]
    );
    assert.equal(view.fileCount, 2);
    const coffee = findNode(view.root, "topics/coffee.md");
    assert.deepEqual([coffee.size, coffee.writes, coffee.content], [11, 2, null]);
    assert.deepEqual(view.source, {
      filesystem: "local-disk",
      config: { directory: "alice" },
      task_queue: "q"
    });
    assert.equal(findNode(view.root, "topics/missing.md"), null);
  });

  it("marks the paths the cursor's commit touched, and their ancestors", () => {
    const [view] = buildMountViews([
      indexDoc([{ op: "replace", path: "/entries/topics~1coffee.md", value: {} }])
    ]);
    assert.equal(findNode(view.root, "topics/coffee.md").changed, true);
    assert.equal(findNode(view.root, "topics").changed, true);
    assert.equal(findNode(view.root, "MEMORY.md").changed, false);
  });

  it("ignores header changes", () => {
    const [view] = buildMountViews([indexDoc([{ op: "replace", path: "/mount", value: "/m" }])]);
    assert.equal(view.root.children.some((n) => n.changed), false);
  });

  it("gives a tree's files their contents and marks directory changes by name", () => {
    const [view] = buildMountViews([
      treeDoc([{ op: "add", path: "/directories/0", value: "memory" }])
    ]);
    const skill = findNode(view.root, "memory/SKILL.md");
    assert.deepEqual(skill.content, { text: "# Memory", encoding: "utf-8" });
    assert.equal(findNode(view.root, "memory").changed, true);
    assert.equal(view.source, null);
  });

  it("changes a node's version whenever what is known about it does", () => {
    const before = findNode(buildMountViews([indexDoc()])[0].root, "MEMORY.md").version;
    const rewritten = indexDoc();
    rewritten.value.entries["MEMORY.md"] = { is_dir: false, size: 40, mtime: null, writes: 2 };
    const after = findNode(buildMountViews([rewritten])[0].root, "MEMORY.md").version;
    assert.notEqual(before, after);
  });

  it("lists a parent's mounts before a subagent's", () => {
    const sub = { ...treeDoc(), key: "wf-2:skills", role: "subagent" };
    const views = buildMountViews([sub, indexDoc()]);
    assert.deepEqual(
      views.map((v) => v.role),
      ["parent", "subagent"]
    );
  });
});

describe("file contents", () => {
  it("chooses a view from the file name", () => {
    assert.deepEqual(fileView("notes/README.md"), { mode: "markdown" });
    assert.deepEqual(fileView("main.py"), { mode: "code", language: "python" });
    assert.deepEqual(fileView("data.JSON"), { mode: "code", language: "json" });
    assert.deepEqual(fileView("Makefile"), { mode: "text" });
  });

  it("decodes text and refuses what is not", () => {
    assert.equal(decodeText(contentBytes("héllo", "utf-8")), "héllo");
    assert.equal(decodeText(contentBytes("AP8=", "base64")), null);
    // A multi-byte character split across two pages still decodes once they are joined.
    const bytes = contentBytes("é", "utf-8");
    const joined = new Uint8Array([...bytes.slice(0, 1), ...bytes.slice(1)]);
    assert.equal(decodeText(joined), "é");
  });
});
