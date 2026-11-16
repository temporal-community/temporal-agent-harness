// ABOUTME: Pins that a sessions list that failed to load says so, with a way to try again. It
// read "SESSIONS 0 · No matching sessions.", which is a claim about the server's data that the
// console had no way of knowing.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import SessionControls from "../src/lib/components/chat/SessionControls.svelte";
import { stripComments } from "./support/controllerHarness.mjs";

const markup = (props) =>
  stripComments(render(SessionControls, { props: { display: "pane", ...props } }).body);

describe("Session Manager list errors", () => {
  it("shows the failure and Retry instead of an empty result", () => {
    const body = markup({ sessions: [], sessionsError: "Sessions failed (500)", onRefreshSessions: () => {} });
    assert.match(body, /role="alert"[\s\S]*Sessions failed \(500\)[\s\S]*Retry/);
    assert.doesNotMatch(body, /No matching sessions/);
  });

  it("says what a notice says beside the list, with a way to dismiss it and no Retry", () => {
    const body = markup({ sessions: [], notice: "The linked session wf-nope isn't in the session manager.", onDismissNotice: () => {}, onRefreshSessions: () => {} });
    assert.match(body, /class="session-error session-notice[^"]*" role="status">\s*<p[^>]*>The linked session wf-nope isn't in the session manager\.<\/p>[\s\S]*?Dismiss/);
    assert.doesNotMatch(body, /Retry/, "retrying would not bring a missing session back");
  });

  it("explains how to begin when no sessions exist", () => {
    assert.match(markup({ sessions: [] }), /No sessions yet/);
    assert.match(markup({ sessions: [] }), /Start one with any of your agents\./);
  });
});
