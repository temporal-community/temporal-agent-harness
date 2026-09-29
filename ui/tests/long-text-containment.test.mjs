// ABOUTME: Pins the CSS that keeps long or unbroken text inside its box. Worst-case data — a
// 130-char argument name, a 2,000-char error token, schema titles with no spaces, a 120-char tool
// name, a 40-line traceback — carried content past a card's edge, off screen with the controls
// beside it, or grew a block without limit. No SSR render can see layout, so this reads the rules.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

const source = (file) => readFileSync(new URL(`../src/lib/${file}`, import.meta.url), "utf8");

/** The declarations of the rule whose selector line is exactly `selector`. */
function rule(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`));
  assert.ok(match, `no rule for ${selector}`);
  return match[1];
}

describe("long text stays inside its container", () => {
  it("caps a call's argument-name column and wraps the names", () => {
    const css = source("components/agent/CallInput.svelte");
    assert.match(rule(css, ".args"), /grid-template-columns: fit-content\(40%\) minmax\(0, 1fr\);/);
    assert.match(rule(css, ".args dt"), /overflow-wrap: anywhere;/);
  });

  it("lets Copyable's text shrink and wrap so its copy button stays on screen", () => {
    const text = rule(source("components/primitives/Copyable.svelte"), ".copyable > :global(:not(.copy))");
    assert.match(text, /min-width: 0;/);
    assert.match(text, /overflow-wrap: anywhere;/);
  });

  it("pins schema form fields to the form's width", () => {
    const css = source("components/chat/SchemaForm.svelte");
    const field = rule(css, ".field");
    assert.match(field, /grid-template-columns: minmax\(0, 1fr\);/);
    assert.match(field, /overflow-wrap: anywhere;/);
    assert.match(rule(css, ".nested"), /min-width: 0;/, "a fieldset's default min size is its content's");
    assert.match(rule(css, ".check"), /display: flex;/, "a long boolean label wraps");
  });

  it("bounds a submit error under its card, scrolling what does not fit", () => {
    const chat = source("components/agent/AgentChatPanel.svelte");
    const callback = source("components/agent/PendingCallbackCard.svelte");
    for (const block of [rule(chat, ".approval-error"), rule(callback, ".error")]) {
      assert.match(block, /max-height: 6lh;/);
      assert.match(block, /overflow-y: auto;/);
      assert.match(block, /overflow-wrap: anywhere;/);
    }
  });

  it("keeps the Logs kind and status chips whole, truncating the tool name instead", () => {
    const css = source("components/agent/TranscriptPanel.svelte");
    assert.match(rule(css, ".line-meta > :global(.chip)"), /flex-shrink: 0;/);
    assert.match(rule(css, ".line-tool"), /min-width: 0;[\s\S]*text-overflow: ellipsis;/);
  });

  it("caps a Logs row's detail blocks, scrolling a long traceback inside them", () => {
    const pre = rule(source("components/agent/TranscriptPanel.svelte"), "pre");
    assert.match(pre, /max-height: 320px;/);
    assert.match(pre, /overflow: auto;/);
  });
});
