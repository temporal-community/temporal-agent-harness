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
  formatSize,
  isFileStateDoc,
  splitFrontMatter
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

const writable = (state) => ({ ...state, value: { ...state.value, read_only: false } });

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
      indexDoc([{ op: "replace", path: "/entries/topics~1coffee.md", before: { writes: 1 }, value: { writes: 2 } }])
    ]);
    assert.equal(findNode(view.root, "topics/coffee.md").change, "modified");
    assert.deepEqual([findNode(view.root, "topics").change, findNode(view.root, "topics").changedBelow], [null, true]);
    assert.equal(findNode(view.root, "MEMORY.md").change, null);
  });

  /* Added, modified and removed all drew one green pip, and a removed file just vanished. */
  it("says how each path changed, and keeps a removed one as what stood there", () => {
    const [view] = buildMountViews([
      indexDoc([
        { op: "add", path: "/entries/MEMORY.md", value: { writes: 1 } },
        { op: "replace", path: "/entries/topics~1coffee.md/writes", before: 1, value: 2 },
        { op: "remove", path: "/entries/old~1gone.md", before: { is_dir: false, size: 7, mtime: null, writes: 1 } }
      ])
    ]);
    assert.equal(findNode(view.root, "MEMORY.md").change, "added");
    assert.equal(findNode(view.root, "topics/coffee.md").change, "modified");
    const old = view.root.children.find((n) => n.name === "old");
    assert.deepEqual([old.change, old.changedBelow], [null, true]);
    assert.deepEqual([old.children[0].change, old.children[0].size], ["removed", 7]);
    assert.equal(findNode(view.root, "old/gone.md"), null, "a removed path is not openable");
    assert.equal(view.fileCount, 2, "a removed file is not counted");
    assert.deepEqual(view.changes, { added: 1, modified: 1, removed: 1 });

    const [tree] = buildMountViews([
      writable(treeDoc([{ op: "remove", path: "/files/memory~1old.py", before: { content: "x", encoding: "utf-8", size: 1 } }]))
    ]);
    const ghost = tree.root.children[0].children.find((n) => n.name === "old.py");
    assert.deepEqual([ghost.change, ghost.content], ["removed", null]);
  });

  /* `new_36.md` sorted before `new_4.md`, and 5 TB printed as "4885957.6 MB". */
  it("sorts names by their numbers and sizes files up to terabytes", () => {
    const doc3 = indexDoc();
    doc3.value.entries = Object.fromEntries(
      ["new_36.md", "new_4.md", "new_12.md"].map((name) => [name, { is_dir: false, size: 1, mtime: null, writes: 1 }])
    );
    const [view] = buildMountViews([doc3]);
    assert.deepEqual(view.root.children.map((n) => n.name), ["new_4.md", "new_12.md", "new_36.md"]);
    assert.equal(formatSize(5_123_456_789_012), "4.7 TB");
    assert.equal(formatSize(3 * 1024 ** 3), "3.0 GB");
    assert.equal(formatSize(0), "0 B");
  });

  /* A SKILL.md the agent only read on the read-only /skills mount showed as "Added". */
  it("marks only writes: a path that was looked up or read has no change", () => {
    const touched = indexDoc([
      { op: "add", path: "/entries/topics~1read.md", value: { is_dir: false, size: 5, mtime: null, writes: 0 } },
      { op: "add", path: "/entries/listed", value: { is_dir: true, size: null, mtime: null, writes: 0 } },
      {
        op: "replace",
        path: "/entries/MEMORY.md",
        before: { is_dir: false, size: 40, mtime: null, writes: 1 },
        value: { is_dir: false, size: 41, mtime: 12.5, writes: 1 }
      },
      {
        op: "replace",
        path: "/entries/topics~1coffee.md",
        before: { is_dir: false, size: 11, mtime: null, writes: 1 },
        value: { is_dir: false, size: 12, mtime: null, writes: 2 }
      },
      { op: "replace", path: "/entries/listed-only/size", before: null, value: 3 }
    ]);
    for (const { path, value } of touched.changed.filter((c) => c.value?.writes !== undefined)) {
      touched.value.entries[path.slice("/entries/".length).replace("~1", "/")] = value;
    }
    const [view] = buildMountViews([touched]);
    assert.equal(findNode(view.root, "topics/read.md").change, null);
    assert.equal(findNode(view.root, "listed").change, null);
    assert.equal(findNode(view.root, "MEMORY.md").change, null);
    assert.equal(findNode(view.root, "listed-only").change, null);
    assert.equal(findNode(view.root, "topics/coffee.md").change, "modified");
    assert.deepEqual(view.changes, { added: 0, modified: 1, removed: 0 });

    const skills = { ...indexDoc(), key: "wf-1:skills" };
    skills.value = { ...skills.value, mount: "/skills", read_only: true };
    skills.changed = [
      { op: "add", path: "/entries/memory", value: { is_dir: true, writes: 0 } },
      { op: "add", path: "/entries/memory~1SKILL.md", value: { is_dir: false, size: 8, writes: 1 } },
      { op: "remove", path: "/entries/old.md", before: { is_dir: false, size: 1, writes: 1 } }
    ];
    const [readOnly] = buildMountViews([skills]);
    assert.equal(readOnly.root.children.some((n) => n.change || n.changedBelow), false);
    assert.deepEqual(readOnly.changes, { added: 0, modified: 0, removed: 0 });
  });

  it("ignores header changes", () => {
    const [view] = buildMountViews([indexDoc([{ op: "replace", path: "/mount", value: "/m" }])]);
    assert.equal(view.root.children.some((n) => n.change || n.changedBelow), false);
  });

  it("gives a tree's files their contents and marks directory changes by name", () => {
    const [view] = buildMountViews([
      writable(treeDoc([{ op: "add", path: "/directories/0", value: "memory" }]))
    ]);
    const skill = findNode(view.root, "memory/SKILL.md");
    assert.deepEqual(skill.content, { text: "# Memory", encoding: "utf-8" });
    assert.equal(findNode(view.root, "memory").change, "added");
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

  it("splits a memory file's front matter from its markdown body, and leaves a malformed one whole", () => {
    const frontMatter = [
      "type: fact",
      "title: Biscuit",
      "description: The user's dog",
      "tags:",
      "  - pets",
      "generated:",
      "  by: code-mode-memory",
      "  at: 2026-10-22T08:00:00Z"
    ].join("\n");
    const file = `---\n${frontMatter}\n---\n# Biscuit\n\nA female corgi.\n`;
    assert.deepEqual(splitFrontMatter(file), { frontMatter, body: "# Biscuit\n\nA female corgi.\n" });
    assert.deepEqual(splitFrontMatter(file.replace(/\n/g, "\r\n")).frontMatter, frontMatter.replace(/\n/g, "\r\n"));

    for (const whole of [
      "# No front matter\n\n---\nkey: value\n---\n",
      "---\nThis is a paragraph between rules.\n---\nbody",
      "---\ntitle: never closed\n\nbody",
      "---\n\n---\nbody",
      "---\n# only a comment\n---\nbody",
      "---\n- a list with no key\n---\nbody",
      "---\ntitle: Biscuit\n- stray item under a value\n---\nbody",
      "---\ntitle: Biscuit\n  nested: under a value\n---\nbody",
      "---\ntags:\n- cat\nJust a sentence.\n---\nbody"
    ]) {
      assert.deepEqual(splitFrontMatter(whole), { frontMatter: null, body: whole }, whole);
    }
  });

  /* The agent writes YAML lists flush left, so pixel.md rendered its front matter as markdown. */
  it("accepts the YAML shapes agents write", () => {
    const shapes = {
      "flush-left list": "type: fact\ntags:\n- cat\n- pet\nverified:\n- by: code-mode-memory\n  at: 2026-10-23",
      "indented list": "tags:\n  - cat\n  -   pet",
      "nested map": "generated:\n  by: code-mode-memory\n  where:\n    host: local",
      "quoted values": `title: "Pixel: the cat"\n'odd key': 'it''s 7'\nempty: ""`,
      comment: "# written by the agent\ntitle: Pixel # the cat\ntags: # none yet\n  - cat",
      "blank lines and a block scalar": "title: Pixel\n\ndescription: >\n  A grey tabby,\n  seven years old.\n\ntags:\n- cat"
    };
    for (const [shape, frontMatter] of Object.entries(shapes)) {
      assert.deepEqual(splitFrontMatter(`---\n${frontMatter}\n---\n# Pixel\n`), { frontMatter, body: "# Pixel\n" }, shape);
    }
    assert.deepEqual(splitFrontMatter("---\ntitle: Pixel\n...\nbody"), { frontMatter: "title: Pixel", body: "body" });
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
