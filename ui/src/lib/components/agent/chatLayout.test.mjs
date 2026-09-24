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

  it("lets only the handler chip give way in the footer toolbar", () => {
    assert.match(rule(chat, ".composer-toolbar :global(.chip)"), /flex-shrink: 0;/);
    assert.match(rule(chat, ".composer-toolbar :global(.target-chip)"), /flex-shrink: 1;/);
  });
});
