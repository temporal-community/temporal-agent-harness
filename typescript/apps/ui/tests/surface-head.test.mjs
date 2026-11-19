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

describe("the page heading", () => {
  it("is one visually hidden h1 that every pane's h2 hangs from", () => {
    assert.equal(shell.match(/<h1\b/g)?.length, 1);
    assert.match(shell, /<h1 class="visually-hidden">Agent console/);
    assert.match(rule(app, ".visually-hidden"), /clip-path: inset\(50%\);/);
  });
});

/* WCAG 1.4.10: at 400% zoom a 1280 x 1024 window is 320 x 256 CSS px. */
describe("the shell at 400% zoom", () => {
  it("lays the Session Manager over the desk when narrow, hiding what it covers, and puts it away on a pick", () => {
    const narrow = between(shell, "@media (max-width: 640px) {", "\n  }\n");
    assert.match(narrow, /\.app\.has-session-drawer:not\(\.bleed\) \{\s*grid-template-columns: minmax\(0, 1fr\);/);
    assert.match(narrow, /grid-column: 1;\s*visibility: hidden;/, "covered, so nothing under it takes focus");
    assert.match(narrow, /> :global\(\.rail-frame\) \{\s*grid-row: 2;/, "named rows, or auto-placement starves the drawer");
    assert.match(shell, /const NARROW_SESSION_DRAWER = "\(max-width: 640px\)";/);
    assert.match(shell, /if \(window\.matchMedia\(NARROW_SESSION_DRAWER\)\.matches\) holdSessionManager\(false\);/);
  });

  it("scrolls the page down when short instead of squeezing the rail", () => {
    const short = between(shell, "@media (max-height: 500px) {", "\n  }\n");
    assert.match(short, /\.app \{\s*height: auto;\s*min-height: 100dvh;/);
    assert.match(short, /> :global\(\.rail-frame\) \{\s*min-height: calc\(100dvh - var\(--head-height\)\);/);
    assert.match(app, /@media \(max-height: 500px\) \{\s*body \{\s*overflow-y: auto;/);
  });
});

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
