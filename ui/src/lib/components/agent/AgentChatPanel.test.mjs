// ABOUTME: Asserts the pending-approval card says nothing about arguments a call does not take.
// A tool like start_monty is gated with tool_input={}, and the detail line rendered that as a
// literal "{}" — a line of type noise under the tool name that tells the approver nothing. The
// reverse direction is pinned too, and it is the one that is easy to break while fixing this:
// suppressing on emptiness alone (`!input`, or a general "looks empty" test) would also swallow
// UNKNOWN_TOOL_INPUT, which is the opposite of nothing to say — it means the model streamed
// arguments that were lost, and an approver deciding on a call must be told that before answering.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import AgentChatPanel from "./AgentChatPanel.svelte";
import { UNKNOWN_TOOL_INPUT } from "$lib/state/logValue.ts";

const approvalRow = (toolId, toolName, input) => ({
  id: toolId,
  index: 0,
  ordinal: 0,
  turnNumber: 1,
  sourceTurnNumber: 1,
  turnId: "turn-1",
  timestamp: 1_700_000_000,
  event: "tool_approval_requested",
  actor: "tool",
  tone: "warn",
  label: "approval requested",
  toolId,
  toolName,
  input
});

const cardHtml = (row) =>
  render(AgentChatPanel, {
    props: {
      items: [],
      logs: [row],
      agentLabel: "Planner",
      sessionId: "wf-root"
    }
  }).body;

describe("the pending-approval card", () => {
  it("shows no detail line for a call that takes no arguments", () => {
    const body = cardHtml(approvalRow("tool-1", "start_monty", {}));

    assert.match(body, /start_monty/, "the gated call should still be named");
    assert.doesNotMatch(body, /pending-approval-detail/, "an empty {} rendered as a detail line");
  });

  it("still shows the arguments a call does take", () => {
    const body = cardHtml(approvalRow("tool-2", "issue_refund", { amount: 42 }));

    assert.match(body, /pending-approval-detail/);
    assert.match(body, /\{ "amount": 42 \}/);
  });

  it("still says so when the arguments are unknown", () => {
    const body = cardHtml(approvalRow("tool-3", "issue_refund", null));

    assert.match(body, /pending-approval-detail/);
    assert.ok(
      body.includes("stream ended before they could be parsed"),
      "lost arguments must be reported, not suppressed as if the call took none"
    );
    assert.ok(UNKNOWN_TOOL_INPUT.startsWith("—"), "the unknown marker is a leading em dash");
  });
});

/* The ID shown beside a copy button is the one it copies: both sit in one `.copyable`. */
describe("chat copy controls", () => {
  it("offers the pending approval's tool call ID, beside that ID", () => {
    const body = stripComments(cardHtml(approvalRow("call_abc123", "issue_refund", { amount: 42 })));
    assert.match(
      body,
      /class="copyable[^"]*">\s*<code[^>]*>call_abc123<\/code>\s*<button[^>]*aria-label="Copy tool call ID"/
    );
  });

  it("offers the error message a failed session shows", () => {
    const body = stripComments(
      render(AgentChatPanel, {
        props: { items: [], logs: [], agentLabel: "Planner", sessionId: "wf-root", error: "Worker lost: boom" }
      }).body
    );
    assert.match(
      body,
      /class="copyable[^"]*">\s*<span[^>]*>Worker lost: boom<\/span>\s*<button[^>]*aria-label="Copy error"/
    );
  });
});
