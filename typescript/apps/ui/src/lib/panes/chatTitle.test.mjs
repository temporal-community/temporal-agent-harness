// ABOUTME: The chat pane's header names a subagent the composer is addressing and nothing
// otherwise: the session's own agent is already in the top bar, so repeating it there was a
// second reading of the same fact. The pane keeps an accessible name either way.

import assert from "node:assert/strict";
import { createRawSnippet } from "svelte";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { createPaneStack } from "../state/paneStack.svelte.ts";
import PaneRail from "./PaneRail.svelte";
import { chatPaneTitle } from "./registry.ts";

const targets = [
  { workflowId: "wf-parent", role: "parent", label: "Wiki Keeper (Callback Tools)" },
  { workflowId: "wf-child", role: "subagent", label: "cleaner (c0ffee)" }
];

const chatHead = (targetId) => {
  const stack = createPaneStack();
  stack.openPane({ kind: "chat" });
  const body = render(PaneRail, {
    props: {
      stack,
      describe: (pane) => ({ title: pane.kind === "chat" ? chatPaneTitle(targets, targetId) : pane.kind }),
      paneContent: createRawSnippet(() => ({ render: () => "<div></div>" }))
    }
  }).body;
  const chat = body.split(/(?=<section class="pane)/).find((part) => /aria-label="Agent chat/.test(part)) ?? "";
  return {
    name: chat.match(/aria-label="([^"]*)"/)?.[1],
    title: chat.match(/class="head-title[^"]*"[^>]*>([^<]*)</)?.[1] ?? null
  };
};

describe("the chat pane's header title", () => {
  it("is empty for the session's own agent, with or without a target chosen", () => {
    for (const targetId of [null, "wf-parent", "wf-gone"]) {
      assert.deepEqual(chatHead(targetId), { name: "Agent chat", title: null }, String(targetId));
    }
  });

  it("names a subagent the composer is addressing", () => {
    assert.deepEqual(chatHead("wf-child"), { name: "Agent chat: cleaner (c0ffee)", title: "cleaner (c0ffee)" });
  });
});
