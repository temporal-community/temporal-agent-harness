// ABOUTME: Asserts what the files pane and its file viewer say about where a file's contents come
// from. The pane shows each tracked mount as a tree; opening a file shows it in a dialog beside the
// same trees. An in-memory mount's file is session state, shown as of the cursor; an indexed
// mount's file is read from its store and is always the file as it is NOW, which the viewer says
// every time, and warns about when the cursor is parked before the live edge. These are server
// renders (no DOM, no effects), so they check the markup only; the store read itself is covered
// by the projection and API tests and the browser check.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import FilesPanel from "./FilesPanel.svelte";
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

  it("says an indexed mount shows only what this session touched, and only for that mount", () => {
    const note = "Showing only what this session has touched, not the whole store.";
    const byMount = Object.fromEntries(
      html({})
        .split('data-mount="')
        .slice(1)
        .map((block) => [block.slice(0, block.indexOf('"')), block])
    );
    assert.ok(byMount["/memory"].includes(note));
    assert.ok(!byMount["/skills"].includes(note));
  });

  it("an indexed mount with nothing touched says so", () => {
    const [index] = buildMountViews([
      doc("memory", {
        kind: "file_index",
        mount: "/memory",
        description: "",
        read_only: false,
        source: null,
        entries: {}
      })
    ]);
    const body = html({ mounts: [index] });
    assert.match(body, /This session hasn(&#39;|')t touched anything in this mount yet/);
  });

  it("shows every mount's tree, and no viewer until a file is opened", () => {
    const body = html({});
    assert.match(body, /\/memory/);
    assert.match(body, /touched this session/);
    assert.match(body, /in memory/);
    assert.match(body, /read-only/);
    assert.match(body, /data-path="memory\/SKILL.md"/);
    assert.equal(viewer(body), "");
  });

  it("opens a file in a dialog beside the trees", () => {
    const dialog = viewer(html({ selected: { mountKey: "wf-1:skills", path: "memory/SKILL.md" } }));
    assert.match(dialog, /class="file-viewer/);
    assert.match(dialog, /<h2[^>]*>SKILL\.md<\/h2>/);
    assert.match(dialog, /data-path="memory\/SKILL.md"[^>]*aria-current="true"|aria-current="true"[^>]*data-path="memory\/SKILL.md"/);
    assert.match(dialog, /data-path="MEMORY.md"/);
  });

  it("shows an in-memory file from state, as of the cursor", () => {
    const dialog = viewer(html({ selected: { mountKey: "wf-1:skills", path: "memory/SKILL.md" } }));
    assert.match(dialog, /As of cursor/);
    assert.match(dialog, /the file as it was at the point in the session you're/);
    assert.match(dialog, /Memory skill/);
    assert.doesNotMatch(dialog, /Live/);
  });

  it("keeps a code file in one block, even when it holds a fence", () => {
    const dialog = viewer(html({ selected: { mountKey: "wf-1:skills", path: "memory/run.py" } }));
    assert.equal(dialog.match(/<pre class="md-code-block"/g)?.length, 1);
    assert.match(dialog, /data-language="Python"/);
    // Highlighting wraps operators and numbers in spans; the fence stays inside the one block.
    assert.match(
      dialog,
      /x <span class="md-syntax-operator">=<\/span> <span class="md-syntax-number">1<\/span>\n```\ny /
    );
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
    assert.match(dialog, /Live/);
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
