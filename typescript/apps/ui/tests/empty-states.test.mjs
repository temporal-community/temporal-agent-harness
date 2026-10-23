// ABOUTME: Pins the two things that made side-by-side panes look broken. Each pane drew its own empty
// state (a bordered card, a bare paragraph, an icon card), so they read as three designs; they now
// all draw EmptyState. And app.css draws a code block's language tag from `pre[data-language]` but
// leaves each host to place it, so a host that forgot (the VFS pane's empty state) printed the tag
// inline over the first line of code. No SSR render can see that layout, so the second half reads
// the rules.

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import AgentStatePanel from "../src/lib/components/agent/AgentStatePanel.svelte";
import ApprovalDecisionPanel from "../src/lib/components/agent/ApprovalDecisionPanel.svelte";
import FilesPanel from "../src/lib/components/agent/FilesPanel.svelte";

const SRC = new URL("../src/", import.meta.url);

describe("pane empty states", () => {
  it("draw the one EmptyState primitive, code included", () => {
    const panes = {
      state: render(AgentStatePanel, { props: { states: [] } }).body,
      files: render(FilesPanel, {
        props: { mounts: [], live: true, onViewFile: async () => ({}) }
      }).body,
      decisions: render(ApprovalDecisionPanel, {
        props: { decisions: [], ahead: 0, onJumpToLive: () => {} }
      }).body
    };
    for (const [kind, body] of Object.entries(panes)) {
      assert.match(body, /class="empty-state[ "]/, `the ${kind} pane drew its own empty state`);
    }
    for (const kind of ["state", "files"]) {
      assert.match(panes[kind], /<pre dir="ltr" data-language="python"/, `${kind}: snippet lost its chrome`);
    }
  });

  it("place the language tag wherever a pre carries data-language", () => {
    const files = readdirSync(SRC, { recursive: true }).filter((f) => f.endsWith(".svelte"));
    const hosts = files.filter((f) => /<pre[^>]*\sdata-language=/.test(readFileSync(new URL(f, SRC), "utf8")));
    assert.ok(hosts.length >= 3, `found only ${hosts.length} code-block hosts`);
    for (const file of hosts) {
      assert.match(
        readFileSync(new URL(file, SRC), "utf8"),
        /::before \{[^}]*position: absolute;/,
        `${file} renders pre[data-language] without placing its tag`
      );
    }
  });
});
