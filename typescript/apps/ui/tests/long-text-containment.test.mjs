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
    assert.match(rule(css, ".args dt"), /-webkit-line-clamp: 3;/, "a long key cannot stretch its row past the value's clamp");
  });

  it("lets Copyable's text shrink and wrap so its copy button stays on screen", () => {
    const text = rule(source("components/primitives/Copyable.svelte"), ".copyable > :global(:not(.copy))");
    assert.match(text, /min-width: 0;/);
    assert.match(text, /overflow-wrap: anywhere;/);
  });

  it("never scrolls the chat's message list sideways", () => {
    /* A 300-char email in a JSON reply scrolls inside its block; what overflowed the list was the
       invisible tooltip of the copy control at the bubble's corner, which still counts as scroll
       overflow. Any wide thing painted past a message's edge would do the same. */
    const list = rule(source("components/agent/AgentChatPanel.svelte"), ".message-list");
    assert.match(list, /overflow-y: auto;/);
    assert.match(list, /overflow-x: hidden;/);
    const code = rule(source("components/chat/JsonReply.svelte"), ".json-code code");
    assert.match(code, /white-space: pre;/);
  });

  it("pins schema form fields to the form's width", () => {
    const css = source("components/chat/SchemaForm.svelte");
    /* Not `.field`: app.css owns that name for search and filter boxes, and its fixed height and
       border clipped every schema field drawn with it. */
    assert.doesNotMatch(css, /class="field"/);
    const field = rule(css, ".schema-field");
    assert.match(field, /grid-template-columns: minmax\(0, 1fr\);/);
    assert.match(field, /overflow-wrap: anywhere;/);
    assert.match(rule(css, ".nested"), /min-width: 0;/, "a fieldset's default min size is its content's");
    assert.match(rule(css, ".check"), /display: flex;/, "a long boolean label wraps");
  });

  it("shrinks a dropdown to its box whatever its longest option, the chevron kept in view", () => {
    /* A native select is as wide as its longest option: a 2,000-char enum value, or a 300-char
       agent name in the drawer's filter, would push the box and its chevron past the pane. */
    const css = readFileSync(new URL("../src/app.css", import.meta.url), "utf8");
    assert.match(rule(css, ".field,\n.form-control"), /min-width: 0;/);
    assert.match(rule(css, ".field > :is(input, select)"), /flex: 1;\s*min-width: 0;/);
    assert.match(rule(css, ".field.select > select"), /text-overflow: ellipsis;\s*white-space: nowrap;/);
    assert.match(rule(css, ".select-chevron"), /position: absolute;\s*inset-inline-end: var\(--gap-md\);/);
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

  it("keeps a trace row's label whole, truncating the tool name instead", () => {
    const css = source("components/primitives/TraceRow.svelte");
    assert.match(rule(css, ".trace-prefix"), /flex: none;[\s\S]*max-width: 100%;/);
    assert.match(rule(css, "strong"), /flex: 0 1 auto;/);
    assert.match(rule(css, ".trace-copy > :is(.trace-prefix, strong, .trace-note)"), /min-width: 0;/);
    for (const part of ["trace-prefix fade-end", "\\{ mono \\}, \"fade-end\"", "trace-note fade-end"]) {
      assert.match(css, new RegExp(part), "a clipped part fades at its end rather than spending a character on an ellipsis");
    }
  });

  it("routes Logs detail blocks through the shared rendered-height clamp", () => {
    const logs = source("components/agent/TranscriptPanel.svelte");
    assert.match(logs, /<LongContent id=\{`log-\$\{row\.id\}/);
    assert.doesNotMatch(rule(logs, "pre"), /max-height:/);
    const pre = rule(source("components/agent/TranscriptPanel.svelte"), "pre");
    assert.match(pre, /overflow: auto;/);
  });

  it("keeps the file tree to the pane's width, ellipsizing long names", () => {
    /* A 480-char file name grew the grid's implicit auto track to 3,280px in a 439px pane, so
       the tree scrolled sideways, no name ever ellipsized, and the mount's chips sat off screen. */
    const css = source("components/agent/MountTrees.svelte");
    assert.match(rule(css, ".mounts"), /grid-template-columns: minmax\(0, 1fr\);/);
    assert.match(rule(css, ".mount"), /grid-template-columns: minmax\(0, 1fr\) auto;/);
    /* A 2,000-character name squeezed the file icon to 0px. */
    assert.match(rule(css, ".row :global(svg)"), /flex: none;/);
    /* Cut in the middle: the stem gives way and the extension or last segment stays. */
    assert.match(rule(css, ".stem"), /min-width: 0;[\s\S]*text-overflow: ellipsis;/);
    assert.match(rule(css, ".tail"), /flex: none;/);
  });
});
