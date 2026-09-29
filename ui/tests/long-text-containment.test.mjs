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
});
