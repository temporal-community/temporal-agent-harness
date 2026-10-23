// ABOUTME: Asserts what the files pane and its file viewer say about where a file's contents come
// from. The pane shows each tracked mount as a tree; opening a file shows it in a dialog beside the
// same trees. An in-memory mount's file is session state, shown as of the cursor; an indexed
// mount's file is read from its store and is always the file as it is NOW, which the viewer says
// every time, and warns about when the cursor is parked before the live edge. These are server
// renders (no DOM, no effects), so they check the markup only; the store read itself is covered
// by the projection and API tests and the browser check.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import FilesPanel from "./FilesPanel.svelte";
import { nodeKey, treeKey, treeRows } from "./MountTrees.svelte";
import OKFGraphDialog from "./OKFGraphDialog.svelte";
import { buildMountViews } from "$lib/state/fileMounts.ts";

const doc = (stateId, value) => ({
  key: `wf-1:${stateId}`,
  stateId,
  workflowId: "wf-1",
  label: "assistant",
  role: "parent",
  version: 1,
  value,
  changed: [],
  commits: 1,
  turnNumber: 1,
  problem: null
});

const HOSTILE = '<script>alert(1)</script><img src=x onerror="alert(2)">';

const mounts = buildMountViews([
  doc("memory", {
    kind: "file_index",
    mount: "/memory",
    description: "Long-term memory.",
    read_only: false,
    source: { filesystem: "local-disk", config: { directory: "alice" }, task_queue: "q" },
    entries: { "MEMORY.md": { is_dir: false, size: 40, mtime: null, writes: 1 } }
  }),
  doc("skills", {
    kind: "file_tree",
    mount: "/skills",
    description: "",
    read_only: true,
    directories: ["memory"],
    files: {
      "memory/SKILL.md": { content: "# Memory skill", encoding: "utf-8", size: 14 },
      "memory/run.py": { content: "x = 1\n```\ny = 2", encoding: "utf-8", size: 15 },
      // A file's contents are whatever a script wrote, so they are untrusted markup.
      "memory/evil.md": { content: HOSTILE, encoding: "utf-8", size: HOSTILE.length },
      "memory/evil.py": { content: HOSTILE, encoding: "utf-8", size: HOSTILE.length }
    }
  })
]);

const html = (props) =>
  render(FilesPanel, {
    props: { mounts, live: true, onViewFile: async () => ({}), ...props }
  }).body;

const viewer = (body) => body.match(/<dialog[\s\S]*<\/dialog>/)?.[0] ?? "";

describe("FilesPanel", () => {
  it("says how to track a mount when none is tracked", () => {
    const body = html({ mounts: [] });
    assert.match(body, /No agent in this run tracks a filesystem mount/);
    assert.match(body, /memory = agent.vfs_mount\("\/memory", LocalDisk/);
  });

  /* Three indexed mounts printed the scope three times as a chip and three times as a sentence. */
  it("says what the tree is a reading of once for the pane, not once per mount", () => {
    const indexed = (id) =>
      doc(id, {
        kind: "file_index",
        mount: `/${id}`,
        description: "",
        read_only: false,
        source: null,
        entries: { "a.md": { is_dir: false, size: 1, mtime: null, writes: 1 } }
      });
    const body = html({ mounts: buildMountViews(["a", "b", "c"].map(indexed)) });
    assert.equal(body.match(/>[^<]*touched this session/gi)?.length, 1, "said once, as text");
    assert.match(body, /class="kicker reading-label[^"]*"[^>]*>Touched this session · at cursor</);
    assert.match(body, /data-tip="An indexed mount lists only the paths this session/);
    assert.doesNotMatch(body, /mount-scope|Showing only what this session/);
  });

  /* Each empty mount said "nothing touched" three ways; the pane now says it once. */
  it("says once when no mount has anything yet", () => {
    const empty = (id) =>
      doc(id, { kind: "file_index", mount: `/${id}`, description: "", read_only: false, source: null, entries: {} });
    const body = html({ mounts: buildMountViews([empty("a"), empty("b")]) });
    assert.equal(body.match(/class="empty-state/g)?.length, 1);
    assert.match(body, /This session hasn(&#39;|')t touched a file in any mount yet/);
    assert.doesNotMatch(body, /touched anything in this mount/);
    assert.match(body, /aria-label="\/a, indexed, 0 files"/);
  });

  /* A model-facing description rendered in full above each tree: 1,000 characters took 251px. */
  it("keeps a mount's kind, access and description in its row's tip, not in the tree", () => {
    const body = stripComments(html({}));
    const memory = body.slice(body.indexOf('data-mount="/memory"'), body.indexOf('data-mount="/skills"'));
    assert.match(memory, /aria-label="\/memory, indexed, 1 file"/);
    assert.match(memory, /class="tip-card[^"]*"[\s\S]*Long-term memory\.[\s\S]*<\/span><\/button>/);
    assert.equal(body.match(/Long-term memory\./g)?.length, 1, "the description is only in the tip");
    assert.match(body, /aria-label="\/skills, in memory, read-only, 4 files"/);
    assert.match(body, /lucide-lock/, "read-only is a lock glyph");
    assert.doesNotMatch(body, /chip-label/, "no kind or access chips");
    assert.match(body, /data-path="memory\/SKILL.md"/);
    assert.equal(viewer(body), "");
  });

  /* A mount of 3,000 files drew all of them: 24,000 nodes in the pane. */
  it("draws only the rows in view, and hides a collapsed folder's rows", () => {
    const files = Object.fromEntries(
      Array.from({ length: 3_000 }, (_, i) => [`dir_${i % 12}/file_${i}.py`, { content: "", encoding: "utf-8", size: 1 }])
    );
    const [big] = buildMountViews([
      doc("big", { kind: "file_tree", mount: "/big", description: "", read_only: false, directories: [], files })
    ]);
    const body = stripComments(html({ mounts: [big] }));
    const rows = body.match(/<button[^>]*class="row\b[^>]*>/g) ?? [];

    assert.ok(rows.length > 0 && rows.length <= 60, `${rows.length} rows drawn for 3,000 files`);
    assert.match(rows[0], /aria-expanded="true"[^>]*data-index="0"[^>]*aria-label="\/big, in memory, 3,000 files"/);
    assert.match(rows[1], /aria-expanded="true"[^>]*data-index="1"[^>]*data-path="dir_0"/);
    assert.match(body, /class="mounts[^"]*" role="tree" aria-label="Mounts" tabindex=/, "the tree takes focus back");

    const all = treeRows([big], new Set());
    assert.equal(all.length, 1 + 12 + 3_000);
    const folded = treeRows([big], new Set([nodeKey(big.key, "dir_0")]));
    assert.equal(folded.length, all.length - 250);
    assert.equal(folded[1].open, false);
    assert.equal(folded[2].node.path, "dir_1");
    assert.deepEqual(treeRows([big], new Set([big.key])).map((row) => row.kind), ["mount"], "a mount folds to its row");
  });

  /* A 5px green pip was the only sign of a change, on every ancestor, and the same for all three. */
  it("says how each file changed with its glyph, color and words, never a pip", () => {
    const [changed] = buildMountViews([
      {
        ...doc("memory", {
          kind: "file_index",
          mount: "/memory",
          description: "",
          read_only: false,
          source: null,
          entries: {
            "new.md": { is_dir: false, size: 1, mtime: null, writes: 1 },
            "notes/edit.md": { is_dir: false, size: 2, mtime: null, writes: 2 }
          }
        }),
        changed: [
          { op: "add", path: "/entries/new.md", value: { writes: 1 } },
          { op: "replace", path: "/entries/notes~1edit.md", before: { writes: 1 }, value: { writes: 2 } },
          { op: "remove", path: "/entries/gone.md", before: { is_dir: false, size: 3, mtime: null, writes: 1 } }
        ]
      }
    ]);
    const body = stripComments(html({ mounts: [changed] }));
    const row = (path) => body.match(new RegExp(`<button[^>]*data-path="${path}"[\\s\\S]*?</button>`))?.[0] ?? "";
    assert.doesNotMatch(body, /changed-pip/);
    assert.match(row("new.md"), /class="row node added[\s\S]*lucide-file-plus[\s\S]*, added</);
    assert.match(row("notes/edit.md"), /class="row node modified[\s\S]*lucide-file-pen[\s\S]*, modified</);
    assert.match(row("gone.md"), /class="row node removed[\s\S]*lucide-file-x[\s\S]*, removed</);
    assert.match(row("gone.md"), /data-tip="Removed by this mount's latest commit at the cursor"/);
    assert.doesNotMatch(row("notes"), /, (added|modified|removed)<|data-tip=/, "a folder isn't marked for what's under it");
    assert.match(body, /Added<\/dt>[\s\S]*Changed<\/dt>[\s\S]*Removed<\/dt>/);
  });

  /* Every row was a Tab stop: Tab reached 38 of 1,800 rows and left the pane, and arrows did nothing. */
  it("is one Tab stop of a tree that says where each row sits, and moves with the arrows", () => {
    const body = stripComments(html({}));
    assert.match(body, /role="tree" aria-label="Mounts"/);
    const items = body.match(/<button role="treeitem"[^>]*>/g) ?? [];
    assert.equal(items.length, treeRows(mounts, new Set()).length, "every row is a treeitem");
    assert.equal(items.filter((item) => /tabindex="0"/.test(item)).length, 1, "one row is the Tab stop");
    assert.match(items[0], /aria-level="1" aria-setsize="2" aria-posinset="1"/);
    assert.match(body, /aria-level="3" aria-setsize="4" aria-posinset="1"[^>]*data-path="memory\/evil\.md"/);

    const rows = treeRows(mounts, new Set());
    const at = (path) => rows.findIndex((row) => row.kind === "node" && row.node.path === path);
    const skills = rows.findIndex((row) => row.key === "wf-1:skills");
    assert.equal(treeKey(rows, 0, "ArrowDown"), 1);
    assert.equal(treeKey(rows, 3, "Home"), 0);
    assert.equal(treeKey(rows, 0, "End"), rows.length - 1);
    assert.equal(treeKey(rows, skills, "ArrowRight"), skills + 1, "into an open mount's first child");
    assert.equal(treeKey(rows, at("memory/run.py"), "ArrowLeft"), at("memory"), "out to the parent folder");
    assert.equal(treeKey(rows, at("memory"), "ArrowLeft"), skills, "and from a folder to its mount");
    assert.equal(treeKey(rows, at("memory/run.py"), "ArrowRight"), null, "a file opens nothing");
    assert.equal(treeKey(rows, 0, "Tab"), undefined, "Tab is the browser's");
  });

  it("opens a file in a dialog beside the trees", () => {
    const dialog = viewer(html({ selected: { mountKey: "wf-1:skills", path: "memory/SKILL.md" } }));
    assert.match(dialog, /class="file-viewer/);
    assert.match(dialog, /<h2 class="head-title[^"]*" title="SKILL\.md">/);
    assert.match(dialog, /data-path="memory\/SKILL.md"[^>]*aria-current="true"|aria-current="true"[^>]*data-path="memory\/SKILL.md"/);
    assert.match(dialog, /data-path="MEMORY.md"/);
  });

  it("shows an in-memory file from state, as of the cursor", () => {
    const dialog = viewer(html({ selected: { mountKey: "wf-1:skills", path: "memory/SKILL.md" } }));
    assert.match(dialog, /the file as it was at the point in the session you're/);
    assert.match(dialog, /Memory skill/);
    assert.doesNotMatch(dialog, /Live/);
  });

  it("keeps a code file in one block, even when it holds a fence", () => {
    const dialog = viewer(html({ selected: { mountKey: "wf-1:skills", path: "memory/run.py" } }));
    assert.equal(dialog.match(/<pre class="md-code-block/g)?.length, 1);
    assert.match(dialog, /class="code-language-tag[^"]*">python</);
    // Highlighting wraps numbers in spans; the fence stays inside the one block.
    assert.match(dialog, /x = <span class="md-syntax-number">1<\/span>\n```\ny /);
  });

  for (const path of ["memory/evil.md", "memory/evil.py"]) {
    it(`escapes markup in a file's contents (${path})`, () => {
      const dialog = viewer(html({ selected: { mountKey: "wf-1:skills", path } }));
      assert.doesNotMatch(dialog, /<script/i);
      assert.doesNotMatch(dialog, /<img/i);
      // Shown as text. A code view highlights each `<` on its own, so check the pieces.
      assert.match(dialog, /&lt;(<\/span>)?script/);
      assert.match(dialog, /&quot;alert\(2\)&quot;/);
    });
  }

  it("says an indexed file is the file as it is now", () => {
    const dialog = viewer(html({ selected: { mountKey: "wf-1:memory", path: "MEMORY.md" } }));
    assert.match(dialog, /the file as it is <strong>now<\/strong>, not as of this point/);
    assert.doesNotMatch(dialog, /earlier point in the session/);
  });

  it("warns when the cursor is behind the live edge", () => {
    const dialog = viewer(
      html({
        live: false,
        onJumpToLive: () => {},
        selected: { mountKey: "wf-1:memory", path: "MEMORY.md" }
      })
    );
    assert.match(dialog, /You're looking at an earlier point in the session/);
    assert.match(dialog, /Jump to latest step/);
  });

  it("says when the open file is not in the mount at the cursor", () => {
    const dialog = viewer(html({ selected: { mountKey: "wf-1:skills", path: "memory/gone.md" } }));
    assert.match(dialog, /isn't in the mount at this point in the session/);
  });
});

// ---------------------------------------------------------------- an OKF bundle's graph


const okfMounts = buildMountViews([
  doc("memory", {
    kind: "file_index",
    mount: "/memory",
    description: "",
    read_only: false,
    okf_version: "0.2",
    source: { filesystem: "local-disk", config: { directory: "alice" }, task_queue: "q" },
    entries: {}
  })
]);

const okfGraph = {
  concepts: [
    {
      id: "people/ana",
      path: "people/ana.md",
      type: "Person",
      title: "Ana",
      description: "The user's sister.",
      tags: ["family"],
      status: "stable",
      trust_tier: "human-reviewed",
      stale: false,
      size: 120,
      problem: null
    },
    {
      id: "evil",
      path: "evil.md",
      type: "Unknown",
      title: HOSTILE,
      description: HOSTILE,
      tags: [],
      status: "stable",
      trust_tier: "unverified",
      stale: false,
      size: 10,
      problem: "no frontmatter: the file doesn't start with a `---` line"
    }
  ],
  links: [{ source: "people/ana", target: "plans/trip", dangling: true }],
  truncated: false,
  walked_at: 1_790_000_000
};

const dialog = (props) =>
  render(OKFGraphDialog, {
    props: {
      mount: okfMounts[0],
      live: true,
      onOkfGraph: async () => okfGraph,
      onViewFile: async () => ({}),
      onClose: () => {},
      cached: { graph: okfGraph, stateVersion: okfMounts[0].stateVersion },
      ...props
    }
  }).body;

describe("OKF graph", () => {
  it("offers the graph only on an OKF bundle mount, and only when it can be walked", () => {
    assert.equal(okfMounts[0].okfVersion, "0.2");
    assert.equal(mounts.find((m) => m.mount === "/memory").okfVersion, null);
    /* The house quiet icon action, not the one filled accent chip in any pane body. */
    const graph = /<button[^>]*class="head-button[^"]*"[^>]*aria-label="Open \/memory graph"/;
    assert.match(html({ mounts: okfMounts, onOkfGraph: async () => okfGraph }), graph);
    assert.match(html({ mounts: okfMounts, onOkfGraph: async () => okfGraph, graphMountKey: "wf-1:memory" }), /class="head-button on"/);
    assert.doesNotMatch(html({ mounts: okfMounts }), /Open \/memory graph/);
    assert.doesNotMatch(html({ onOkfGraph: async () => okfGraph }), /Open \/memory graph/);
  });

  it("says the graph is the whole bundle, now", () => {
    const body = dialog({});
    assert.match(body, /\/memory knowledge graph/);
    /* One link read "1 links", and the e2e run's one-concept bundle "1 concepts". */
    assert.match(body, /2 concepts · 1 link\b/);
    const one = { ...okfGraph, concepts: okfGraph.concepts.slice(0, 1), links: [] };
    assert.match(dialog({ cached: { graph: one, stateVersion: okfMounts[0].stateVersion } }), /1 concept · 0 links/);
    assert.match(body, /The whole bundle as it is in its store <strong>now<\/strong>/);
    assert.match(body, /not only the ones this session touched/);
    assert.doesNotMatch(body, /earlier point in the session/);
    assert.match(dialog({ live: false, onJumpToLive: () => {} }), /You're looking at an earlier point/);
  });

  it("walks again only when asked", () => {
    assert.match(dialog({}), /aria-label="Reload graph"/);
  });

  /* The stage was an empty box while the bundle was read; an "off" type filter was 45% opacity. */
  it("says it is reading in the stage, and filters by type with the shared chip", () => {
    /* A server render runs no effects, so no walk starts: read the branch from the source. */
    const source = readFileSync(new URL("./OKFGraphDialog.svelte", import.meta.url), "utf8");
    assert.match(source, /\{:else if loading && !graph\}\s*<p class="hint">Reading the bundle…<\/p>/);
    const body = stripComments(dialog({}));
    assert.match(body, /<label class="field[^"]*"><svg[^>]*lucide-search/);
    assert.match(body, /<button[^>]*aria-pressed="true"[^>]*class="chip xs quiet[^"]*active/);
    assert.doesNotMatch(source, /opacity: 0\.45|class="type"|\.type\.off/, "no hand-rolled filter with a disabled look");
  });

  it("draws every concept, and a ghost for a link to one not written yet", () => {
    const body = dialog({});
    assert.match(body, /data-concept="people\/ana"/);
    assert.match(body, /data-concept="plans\/trip"/);
    assert.match(body, /aria-label="trip \(not written yet\)"/);
  });

  it("shows a selected concept's facts and problem, escaped", () => {
    const body = dialog({ selectedId: "evil" });
    assert.doesNotMatch(body, /<script/i);
    assert.doesNotMatch(body, /<img/i);
    assert.match(body, /&lt;script>|&lt;script&gt;/);
    assert.match(body, /no frontmatter: the file doesn't start with a `---` line|no frontmatter: the file doesn&#39;t start/);
    const ana = dialog({ selectedId: "people/ana" });
    assert.match(ana, /Confirmed by a person/);
    assert.match(ana, /The user's sister\.|The user&#39;s sister\./);
    assert.match(ana, /Links to/);
  });

  it("says when a selected concept hasn't been written yet", () => {
    assert.match(dialog({ selectedId: "plans/trip" }), /Not written yet/);
  });
});
