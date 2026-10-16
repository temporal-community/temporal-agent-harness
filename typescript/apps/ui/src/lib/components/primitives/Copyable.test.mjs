// ABOUTME: Pins the copy control put beside an ID. It is a real, named button — keyboard-reachable
// and announced by what it copies — and it tells the reader when a copy did not happen: there is
// no navigator.clipboard outside a secure context, so a console opened over plain http on anything
// but localhost has none, and a browser may refuse the write. Either is a false, never a throw.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { afterEach, describe, it, vi } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import SessionControls from "$lib/components/chat/SessionControls.svelte";
import Copyable, { copyText } from "./Copyable.svelte";

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
