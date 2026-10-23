// ABOUTME: Pins that a session's status word and its hue come from one ordered rule. The word and
// the kind used to be picked by two if-chains in different orders, so an approval pending while the
// stream reconnected drew "Connecting" in the approval hue, and an error mid-send said "Thinking".

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { beforeEach, describe, it } from "vitest";

import SessionControls from "../src/lib/components/chat/SessionControls.svelte";
import { CONNECTING_HINT_AFTER_S, sessionHeadline } from "../src/lib/state/sessionHistory.ts";
import { installBrowserSurface, session, stripComments } from "./support/controllerHarness.mjs";

const open = session("wf-status");

/** The launcher's spoken (or screen-reader-only) word and the hue its kicker takes. */
function anchor(props) {
  const body = stripComments(
    render(SessionControls, {
      props: { display: "launcher", sessions: [open], sessionId: open.workflow_id, ...props }
    }).body
  );
  const word = body.match(/class="(?:kicker session-state|session-state-spoken)[^"]*">([^<]*)</)?.[1];
  const tone = body.match(/--head-accent: var\(--([a-z-]+)\)/)?.[1];
  return { word, tone };
}

describe("the session's status word and hue", () => {
  beforeEach(() => {
    installBrowserSurface();
  });

  it("names a pending approval while the stream reconnects, in the approval hue", () => {
    assert.deepEqual(anchor({ connecting: true, pendingLabel: "1 approval needed" }), {
      word: "1 approval needed",
      tone: "live"
    });
  });

  it("says an error mid-send needs attention, in the error hue", () => {
    assert.deepEqual(anchor({ sending: true, error: "boom" }), {
      word: "Needs attention",
      tone: "error"
    });
  });

  it("ranks closed, error, a waiting person, starting, connecting, then thinking", () => {
    const all = {
      closed: true,
      error: true,
      pendingLabel: "1 response needed",
      creating: true,
      connecting: true,
      sending: true
    };
    const order = ["closed", "error", "pendingLabel", "creating", "connecting", "sending"];
    const seen = order.map((_, i) => {
      const state = { ...all };
      for (const key of order.slice(0, i)) state[key] = key === "pendingLabel" ? null : false;
      return sessionHeadline(state);
    });
    assert.deepEqual(seen, [
      { kind: "closed", label: "Closed" },
      { kind: "error", label: "Needs attention" },
      { kind: "approval", label: "1 response needed" },
      { kind: "starting", label: "Starting" },
      { kind: "connecting", label: "Connecting" },
      { kind: "thinking", label: "Thinking" }
    ]);
    const quiet = Object.fromEntries(order.map((key) => [key, key === "pendingLabel" ? null : false]));
    assert.deepEqual(sessionHeadline(quiet), { kind: "available", label: "Available" });
  });

  /* The row and card said "Needs attention · failed" with the error glyph; the nav said a gray "CLOSED". */
  it("says a failed session needs attention in the nav, as its row and card do", () => {
    const quiet = { closed: true, error: false, pendingLabel: null, creating: false, connecting: false, sending: false };
    assert.deepEqual(sessionHeadline({ ...quiet, failed: true }), { kind: "error", label: "Needs attention" });
    assert.equal(sessionHeadline({ ...quiet, failed: true, gone: true }).label, "Unavailable", "gone still wins");
    const failed = { ...open, execution_status: "TERMINATED", closed: true };
    const body = stripComments(render(SessionControls, { props: { display: "launcher", sessions: [failed], sessionId: failed.workflow_id, closed: true } }).body);
    assert.match(body, /class="kicker session-state[^"]*">Needs attention</);
    assert.match(body, /--head-accent: var\(--error\)/);
    assert.match(body, /class="session-glyph[^"]*"[^>]*--status-color: var\(--error\)[^>]*>[\s\S]*?lucide-circle-x/, "the row's own glyph and tone");
  });

  /* The registry already said "no_worker", yet the nav asked whether the worker was running and
     every other row read a plain "Open". */
  it("says No worker wherever the registry reports none, opened or not", () => {
    const quiet = { closed: false, error: false, pendingLabel: null, creating: false, connecting: true, sending: false };
    assert.deepEqual(sessionHeadline({ ...quiet, noWorker: true, taskQueue: "billing-agent" }), {
      kind: "stuck",
      label: "No worker",
      hint: "No worker is polling this agent's task queue billing-agent. Start the worker to continue."
    });
    assert.equal(sessionHeadline({ ...quiet, closed: true, noWorker: true }).label, "Closed", "a closed session needs no worker");

    const agents = [{ workflow_type: open.agent_workflow_type, label: "Billing", task_queue: "billing-agent", worker: { status: "no_worker" } }];
    const other = { ...open, workflow_id: "wf-other" };
    const nav = anchor({ agents });
    assert.deepEqual(nav, { word: "No worker", tone: "live" });
    const pane = stripComments(render(SessionControls, { props: { display: "pane", agents, sessions: [open, other], sessionId: open.workflow_id } }).body);
    for (const id of [open.workflow_id, "wf-other"]) {
      const row = pane.split('<div class="session-item').find((chunk) => chunk.includes(`id="session-option-${id}"`));
      assert.match(row, /<span class="visually-hidden[^"]*">, No worker,<\/span>/, id);
      assert.match(row, /lucide-triangle-alert|lucide-alert-triangle/, id);
    }
  });

  it("counts a connection up, and past the threshold asks after the agent's worker", () => {
    const connecting = (connectingFor, taskQueue) =>
      sessionHeadline({
        closed: false,
        error: false,
        pendingLabel: null,
        creating: false,
        connecting: true,
        sending: false,
        connectingFor,
        taskQueue
      });
    assert.deepEqual(connecting(0.4), { kind: "connecting", label: "Connecting" });
    assert.deepEqual(connecting(Number.NaN), { kind: "connecting", label: "Connecting" });
    assert.deepEqual(connecting(CONNECTING_HINT_AFTER_S - 0.1), { kind: "connecting", label: "Connecting · 9s" });
    assert.deepEqual(connecting(CONNECTING_HINT_AFTER_S, "openai-hello"), {
      kind: "stuck",
      label: "Connecting · 10s",
      hint: "Still connecting — is this agent's worker running? It polls task queue openai-hello."
    });
    assert.equal(connecting(75).label, "Connecting · 1m 15s");
    assert.equal(connecting(12, null).hint, "Still connecting — is this agent's worker running?");
  });

  it("keeps the connecting hint off the nav line and announces it once", () => {
    const markup = (props) =>
      stripComments(
        render(SessionControls, {
          props: { display: "launcher", connecting: true, connectingSince: Date.now() - 12_000, ...props }
        }).body
      );
    const textOf = (html) => [...html.matchAll(/>([^<]*)</g)].map((match) => match[1]).join("\n");
    const tips = (html) =>
      [...html.matchAll(/data-tip="([\s\S]*?)"/g)].map((match) => match[1]);
    const hintCopies = (html) => html.match(/Still connecting/g) ?? [];

    const unmatched = markup({ sessions: [], sessionId: "wf-missing" });
    const unmatchedText = textOf(unmatched);
    assert.match(unmatchedText, /Connecting · 12s/);
    assert.doesNotMatch(unmatchedText, /No session/, "a missing session is the connecting status, not a second label");
    assert.doesNotMatch(unmatched, /session-hint|aria-live/);
    assert.equal(hintCopies(unmatchedText).length, 1, "the hint is one text node, not a visible copy plus another");
    assert.match(unmatched, /id="session-status-hint"/);
    assert.match(unmatched, /role="tooltip"/);
    assert.match(unmatched, /aria-describedby="session-status-hint"/);
    assert.equal(tips(unmatched).filter((tip) => tip.includes("Still connecting")).length, 0);

    const matched = markup({ sessions: [open], sessionId: open.workflow_id });
    const matchedText = textOf(matched);
    assert.match(matchedText, /Incident Triage Workflow/);
    assert.match(matchedText, /Connecting · 12s/);
    assert.doesNotMatch(matchedText, /No session/);
    assert.equal(hintCopies(matchedText).length, 1);
    assert.doesNotMatch(matched, /session-hint/);
  });
});
