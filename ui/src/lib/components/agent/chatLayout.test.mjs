// ABOUTME: Pins the chat pane's CSS that no SSR render can see. Each grid in the chat pins its one
// column to `minmax(0, 1fr)`: left implicit, the column is `auto` and grows to its widest child's
// min-content — an approval card, a 600px reply table — which carried the whole message column
// 75px past a 403px pane. A wide table has to scroll inside its own wrapper instead.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

const source = (file) => readFileSync(new URL(file, import.meta.url), "utf8");
const chat = source("./AgentChatPanel.svelte");
const markdown = source("../chat/MarkdownMessage.svelte");

/** The declarations of the rule whose selector line is exactly `selector`. */
function rule(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`));
  assert.ok(match, `no rule for ${selector}`);
  return match[1];
}

describe("the chat pane stays inside its pane", () => {
  it("pins every single-column grid to the pane's width", () => {
    for (const selector of [
      ".chat-shell",
      ".pending-approvals",
      ".pending-approval-card",
      ".activity-feed"
    ]) {
      assert.match(rule(chat, selector), /grid-template-columns: minmax\(0, 1fr\);/, selector);
    }
    assert.match(
      rule(markdown, ".markdown-message"),
      /grid-template-columns: minmax\(0, 1fr\);/
    );
  });

  it("scrolls a wide table inside its wrapper", () => {
    const wrap = rule(markdown, ".markdown-message :global(.md-table-wrap)");
    assert.match(wrap, /max-width: 100%;/);
    assert.match(wrap, /overflow-x: auto;/);
  });

  it("draws the replay strip opaque, over the messages and out of flow", () => {
    const strip = rule(chat, ".replay-strip");
    assert.match(strip, /position: absolute;/);
    assert.match(strip, /background: var\(--surface-0\);/, "the chat's own background, no alpha");
    assert.match(strip, /z-index: 2;/);
    assert.match(rule(chat, ".agent-chat"), /background: var\(--surface-0\);/);
    assert.match(rule(chat, ".message-list"), /isolation: isolate;/);
  });

  it("keeps a Code Mode child's HOST CALL chip whole, and its tool name on screen", () => {
    assert.match(rule(chat, ".activity-row-button .activity-copy > :global(.chip)"), /flex-shrink: 0;/);
    assert.match(rule(chat, ".agent-chat"), /container: agent-chat \/ inline-size;/);
    const narrow = chat.match(/@container agent-chat \(max-width: 480px\) \{([\s\S]*?)\n {2}\}/)?.[1];
    assert.ok(narrow, "a narrow-chat rule for nested rows");
    assert.match(narrow, /\.activity-row\.nested \{[^}]*margin-left: 4px;/, "a smaller indent");
    assert.match(narrow, /\.activity-row\.nested \.activity-copy \{[^}]*flex-wrap: wrap;/);
  });

  /* The pane clips its bottom edge, so a row that outgrows it is gone for good: one long
     ask_user question, or 12 approvals and 12 callbacks, pushed the cards' own Send buttons
     and the composer below it, and the transcript collapsed to 28px. */
  it("keeps the composer and every pending card's actions inside the pane", () => {
    const shell = rule(chat, ".agent-chat.headerless .chat-shell");
    assert.match(
      shell,
      /grid-template-rows: minmax\(min\(96px, 25%\), 1fr\) auto minmax\(0, auto\) auto;/,
      "the transcript keeps a floor and only the pending row can shrink below its content"
    );
    assert.match(shell, /grid-template-areas: "messages" "banner" "pending" "composer";/);
    for (const [selector, area] of [
      [".message-list", "messages"],
      [".error-banner", "banner"],
      [".closed-banner", "banner"],
      [".pending-stack", "pending"],
      [".composer-wrap", "composer"]
    ]) {
      assert.match(rule(chat, selector), new RegExp(`grid-area: ${area};`), selector);
    }
    const stack = rule(chat, ".pending-stack");
    assert.match(stack, /min-height: 0;/);
    assert.match(stack, /overflow-y: auto;/);
    assert.match(
      chat,
      /<div class="pending-stack">[\s\S]*?"Pending tool approvals"[\s\S]*?"Pending callback requests"[\s\S]*?<\/div>\s*\{\/if\}\s*<div class="composer-wrap">/,
      "both pending sections scroll together inside the one stack"
    );
    assert.match(rule(chat, ".error-banner"), /max-height: 6lh;[\s\S]*overflow-y: auto;/);
  });

  it("lets only the handler chip give way in the footer toolbar", () => {
    assert.match(rule(chat, ".composer-toolbar :global(.chip)"), /flex-shrink: 0;/);
    assert.match(rule(chat, ".composer-toolbar :global(.target-chip)"), /flex-shrink: 1;/);
  });
});
