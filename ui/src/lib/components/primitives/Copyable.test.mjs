// ABOUTME: Pins the copy control put beside an ID. It is a real, named button — keyboard-reachable
// and announced by what it copies — and it tells the reader when a copy did not happen: there is
// no navigator.clipboard outside a secure context, so a console opened over plain http on anything
// but localhost has none, and a browser may refuse the write. Either is a false, never a throw.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { afterEach, describe, it, vi } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import SessionControls from "$lib/components/chat/SessionControls.svelte";
import Copyable, { OUTCOME_MS, copyText, outcomeLabel, selectText } from "./Copyable.svelte";

afterEach(() => vi.unstubAllGlobals());

describe("Copyable", () => {
  it("renders a named button beside what it copies", () => {
    const { body } = render(Copyable, {
      props: { value: "wf-123", label: "Copy workflow ID" }
    });
    assert.match(body, /<button[^>]*type="button"/);
    assert.match(body, /aria-label="Copy workflow ID"/);
  });

  it("copies the value when there is a clipboard", async () => {
    const written = [];
    vi.stubGlobal("navigator", { clipboard: { writeText: async (text) => written.push(text) } });
    assert.equal(await copyText("wf-123"), true);
    assert.deepEqual(written, ["wf-123"]);
  });

  it("reports a copy that did not happen instead of throwing", async () => {
    vi.stubGlobal("navigator", {});
    assert.equal(await copyText("wf-123"), false, "no clipboard outside a secure context");

    vi.stubGlobal("navigator", {
      clipboard: { writeText: async () => Promise.reject(new DOMException("denied")) }
    });
    assert.equal(await copyText("wf-123"), false, "a refused write");
  });

  /* A failure used to be a 1.5 s hover tip and nothing else: the icon did not change, nothing
     was announced, and the reader was left with no way to get the text. */
  it("shows a failure, announces it, and selects the text to copy by hand", () => {
    assert.equal(outcomeLabel("failed", "Copy JSON", true, "⌘C"), "Copy failed — selected, press ⌘C");
    assert.equal(outcomeLabel("failed", "Copy JSON", false, "⌘C"), "Copy failed");
    assert.equal(outcomeLabel("idle", "Copy JSON", false, "⌘C"), "Copy JSON");
    assert.ok(OUTCOME_MS.failed >= 3000, "a failure holds for a few seconds, not a blink");

    const source = readFileSync(new URL("./Copyable.svelte", import.meta.url), "utf8");
    assert.match(source, /\{:else if outcome === "failed"\}\s*<CircleAlert/, "its own icon, not the copy glyph");

    const { body } = render(Copyable, { props: { value: "wf-123", label: "Copy workflow ID" } });
    assert.match(body, /<\/span>\s*<span class="announce[^"]*" role="status"><\/span>/, "a polite live region beside the control");

    const selected = [];
    vi.stubGlobal("getSelection", () => ({ selectAllChildren: (node) => selected.push(node) }));
    const node = { textContent: "{}" };
    assert.equal(selectText(node), true);
    assert.deepEqual(selected, [node]);
    assert.equal(selectText(null), false, "nothing to select");
  });
});

describe("workflow ID copy spots", () => {
  const markup = (props) => stripComments(render(SessionControls, { props }).body);
  const sessions = [
    { workflow_id: "wf-alpha", created_at: 2, label: "a", agent_workflow_type: "Planner" },
    { workflow_id: "wf-beta", created_at: 1, label: "b", agent_workflow_type: "Planner" }
  ];

  it("keeps session identity and history free of workflow ID copy controls", () => {
    for (const display of ["launcher", "pane"]) {
      const body = markup({ sessions, sessionId: "wf-alpha", display });
      assert.doesNotMatch(body, /aria-label="Copy workflow ID"/);
      assert.doesNotMatch(body, /class="session-id[\s"]/);
    }
  });
});
