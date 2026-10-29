// Pins the one header row that panes and the Session Manager drawer share. The drawer's header
// was its own 28px row of a 13px heading and a boxed IconButton, inset by the drawer's padding,
// so beside a 37px full-bleed pane header the tops, heights and hairlines disagreed. No SSR
// render can see layout, so this reads the rules: both headers take `.surface-head`, and only
// app.css says what that is. The top bar was a 45px row of bordered chips above them, and
// takes the same row now.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

const read = (file) => readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
const app = read("app.css");
const pane = read("lib/panes/PaneShell.svelte");
const controls = read("lib/components/chat/SessionControls.svelte");
const manager = controls.slice(controls.indexOf('<section class="session-manager"'));
const minimap = read("lib/panes/PaneMinimap.svelte");
const shell = read("App.svelte");
const between = (text, open, close) => text.slice(text.indexOf(open), text.indexOf(close, text.indexOf(open)));
const lead = between(shell, "{#snippet lead()}", "{/snippet}");
const launcher = between(controls, '<div class="session-controls">', "{#if newSessionOpen}");
const bar = between(minimap, '<nav class="surface-head minimap"', "</nav>");

/** The declarations of the rule whose selector line is exactly `selector`. */
function rule(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`));
  assert.ok(match, `no rule for ${selector}`);
  return match[1];
}

describe("the shared header row", () => {
  it("is sized by one token, full-bleed, with the hairline", () => {
    const head = rule(app, ".surface-head");
    assert.match(head, /height: var\(--head-height\);/);
    assert.match(head, /padding: 0 var\(--gutter\);/);
    assert.match(head, /border-bottom: 1px solid var\(--border\);/);
  });

  it("heads every pane and the Session Manager, with the same label and buttons", () => {
    assert.match(pane, /<header\s+class="surface-head pane-head"/);
    assert.match(pane, /<h2 class="head-name">\s*<span class=\{\["kicker head-kind"/);
    assert.match(manager, /<header class="surface-head">/);
    assert.match(manager, /<h2 class="head-name"><span class="kicker head-kind">Sessions<\/span>/);
    assert.match(manager, /<button[^>]*class=\{\["head-button session-refresh"[^>]*aria-label="Refresh sessions"[^>]*data-tip="Refresh sessions"/);
    assert.doesNotMatch(manager.slice(0, manager.indexOf("</header>")), /<IconButton/, "refresh is a quiet head button, not a boxed one");
  });

  it("is restated by neither, so the two cannot drift apart", () => {
    assert.doesNotMatch(rule(pane, ".pane-head"), /\b(height|padding|border-bottom|background):/);
    for (const selector of [".head-name", ".head-kind", ".head-title", ".head-button"]) {
      assert.doesNotMatch(pane + controls, new RegExp(`\\n\\s*\\${selector} \\{`), `${selector} belongs to app.css`);
    }
  });

  it("keeps one unchanged hairline between every pane head and body", () => {
    assert.doesNotMatch(rule(pane, ".pane.focused .pane-head"), /\b(?:border|box-shadow)\b/);
    assert.doesNotMatch(rule(pane, ".pane-body"), /\bborder-top\b/);
  });

  it("is the top bar's row too, with quiet icon buttons and no boxed chips", () => {
    assert.ok(bar, "the top bar takes .surface-head");
    assert.doesNotMatch(rule(minimap, ".minimap"), /\b(height|padding|border-bottom|background):/);
    assert.match(lead, /<button\s+type="button"\s+class=\{\["head-button session-drawer-trigger"[^>]*aria-label="Sessions"[^>]*aria-pressed=[^>]*aria-expanded=[^>]*aria-controls="session-manager-drawer"/);
    assert.match(launcher, /<button\s+type="button"\s+class="head-button session-new"\s+aria-label="New session"/);
    assert.match(launcher, /<a\s+class="head-button session-temporal"[^>]*aria-label="Open in Temporal Web UI"/);
    assert.match(launcher, /class="head-name session-anchor"[\s\S]*class="kicker head-kind session-name"/);
    for (const [name, markup] of [["lead", lead], ["launcher", launcher], ["minimap", bar]]) {
      assert.doesNotMatch(markup, /<(Chip|IconButton)\b/, `the ${name} draws no bordered chip or boxed icon`);
    }
  });

  it("starts at the drawer's top edge, with the inset on the body below it", () => {
    assert.doesNotMatch(rule(controls, ".session-manager"), /padding:/);
    assert.match(rule(controls, ".session-body"), /padding: var\(--pane-inset\);/);
    assert.match(rule(controls, ".session-body"), /min-height: 0;/, "the list scrolls under a header that stays");
  });
});
