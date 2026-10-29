// ABOUTME: Source-level conventions for the console, scanned with plain regexes over src/: raw HTML
// is rendered only by the three components whose output reply-xss.test.mjs attacks, agent and model
// prose is drawn by MarkdownMessage or read through markdownToText, only SafeImage writes an <img>,
// every data-built link goes through safeHref, each TanStack package in the bundle was added on
// purpose, card panes share one inset, a pane scroller's track is its end inset, no pane repeats its header's kind as a heading, a recipe with a shared home
// (the warning callout, the jump chip) is not copied back out of it, selection is a tint, never a
// one-sided stroke, every search or filter field in a pane is the shared .field, and motion runs on
// the app.css tokens, names its properties, has a reduced-motion path, and blurs only the queue.
// Each allowlist is also checked for stale entries, so it can only shrink.

import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { join, relative } from "node:path";
import { describe, it } from "vitest";
import { stripComments } from "./support/controllerHarness.mjs";

const root = new URL("../src", import.meta.url).pathname;

async function collect(dir) {
  const files = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) files.push(...(await collect(path)));
    else if (/\.(svelte|ts)$/.test(entry.name) && !/\.test\./.test(entry.name)) files.push(path);
  }
  return files;
}

const sources = await Promise.all(
  (await collect(root)).map(async (path) => ({ file: relative(root, path), text: await readFile(path, "utf8") }))
);

/* Their escaping and URL filtering are what tests/reply-xss.test.mjs attacks with hostile replies. */
const HTML_ALLOWED = [
  "lib/components/chat/JsonReply.svelte",
  "lib/components/chat/MarkdownMessage.svelte",
  "lib/components/flow/AgentStateFlow.svelte"
];

/* A dynamic href is fine when it calls safeHref inline; otherwise its exact expression is listed here. */
const HREF_ALLOWED = [
  { file: "lib/components/agent/TranscriptPanel.svelte", expr: "link.url", why: "comes from citationLinks, which runs safeHref" },
  { file: "lib/components/chat/SessionControls.svelte", expr: "temporalHref", why: "temporalWorkflowUrl: an http(s) base plus an encoded workflow id" },
  { file: "lib/components/primitives/Chip.svelte", expr: "href", why: "forwards its prop; every caller's href is scanned here" },
  { file: "lib/components/chat/MarkdownMessage.svelte", expr: "href", why: "citation markup from citationLinks" },
  { file: "lib/components/chat/MarkdownMessage.svelte", expr: "escapeAttribute(href)", why: "link markup from safeHref" }
];
const HREF = /\bhref=\{([^}]*)\}|\{(href)\}|\bhref="[^"]*\$?\{([^}]*)\}/g;

/* Each one adds to the bundle; check its weight in a `vite build` and add it here deliberately. */
const TANSTACK_ALLOWED = ["@tanstack/charts", "@tanstack/highlight", "@tanstack/markdown", "@tanstack/pacer", "@tanstack/svelte-virtual"];
const TANSTACK_IMPORT = /(?:\bfrom\s*|\bimport\s*\(?\s*)["'](@tanstack\/[a-z0-9-]+)/g;

/* Panes that draw their own cards. Each insets them by the one pane gutter, so a card's stroke is
   never on the pane's border and a pane beside Logs lines up with it. Full-bleed panes (graph,
   chat, latency, usage) own their edges and are not listed. */
const INSET_PANES = [
  { file: "lib/components/agent/AgentStatePanel.svelte", selector: ".agent-state" },
  { file: "lib/components/agent/FilesPanel.svelte", selector: ".files" },
  { file: "lib/components/agent/ApprovalDecisionPanel.svelte", selector: ".approval-decisions" },
  { file: "lib/components/agent/TranscriptPanel.svelte", selector: ".transcript-controls" },
  { file: "lib/components/agent/TranscriptPanel.svelte", selector: ".items" }
];

/* Recipes that have one home in app.css or a primitive; a copy anywhere else is drift. */
const RECIPES_WITH_A_HOME = [
  { pattern: /color-mix\(in srgb, var\(--warning\) 40%, var\(--border\)\)/, home: "the .callout-warning class in app.css" },
  { pattern: /\n\s*\.jump\s*\{/, home: 'Chip (`<Chip label="Jump to latest step" tone="accent" size="xs" onclick=… />`)' },
  { pattern: /\n\s*\.graph-button\s*\{/, home: "the .head-button class in app.css" }
];

/* One-sided strokes that are structure, not state. Selected, open, current and changed rows use
   the --selected-bg tint instead; a hairline divider (1px of --border or --border-strong) passes. */
const SIDE_STROKE_ALLOWED = [
  { file: "lib/components/chat/MarkdownMessage.svelte", selector: ".markdown-message :global(blockquote)", why: "a markdown quote" }
];
const SIDE_BORDER = /\bborder-(?:left|right|inline-start|inline-end)(?:-color|-width|-style)?\s*:\s*([^;]+)/g;
const DIVIDER = /^(?:0|none|1px solid var\(--border(?:-strong)?\))$/;
const SIDE_SHADOW = /\binset\s+-?(?!0(?:px)?\s)[\d.]+(?:px|rem)\s+0(?:px)?\b/;
const EDGE_BAR = (body) =>
  /(?:^|;)\s*(?:width|inline-size)\s*:\s*[2-4]px/.test(body) && /\b(?:left|right|inset-inline-start|inset-inline-end)\s*:\s*0(?:px)?\s*;/.test(body);
const cssRules = (css) =>
  [...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selector, body]) => ({ selector: selector.trim(), body }));

/* Search and filter fields: an input that searches (type="search", or named or placeholdered
   "Search…") and every select. Each sits directly in a .field (app.css), which owns the box. */
const FIELD_CONTROL = /<(input|select)\b([^>]*)>/g;
const isFieldControl = (tag, attrs) => tag === "select" || /type="search"|(?:placeholder|aria-label)="Search/.test(attrs);
const FIELD_ALLOWED = [
  { file: "lib/components/primitives/Select.svelte", why: "the shared dropdown; its root element is the .field" }
];
const OWN_BOX = /(?:^|;)\s*(?:height|border(?:-width|-color)?|box-shadow|outline)\s*:\s*(?!0\s*;|none\s*;|0$|none$)/;

/* Fields that carry prose an agent, a model or a person wrote, so they may hold markdown. In markup
   each is drawn by MarkdownMessage (`text={…}`) or read through markdownToText, never raw. */
const PROSE_FIELD = /\.(?:description|reason|question|preview|initial_user_message)\b|\bthought\.text\b/;
const MARKUP_EXPRESSION = /(\b[\w-]+=)?\{([^{}]*)\}/g;
const markupOf = (text) => text.replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/g, "");

const ruleOf = (text, selector) =>
  text.match(new RegExp(`\\n\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} \\{([^}]*)\\}`))?.[1] ?? null;

const styleOf = (text) => [...text.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join("\n");
const motionSources = async () => [
  { file: "app.css", text: await readFile(join(root, "app.css"), "utf8"), css: true },
  ...sources.filter(({ file }) => file.endsWith(".svelte"))
];

const HOVER_STILL_FILES = [
  "app.css",
  "lib/components/agent/AgentChatPanel.svelte",
  "lib/components/agent/MountTrees.svelte",
  "lib/components/chat/SessionControls.svelte",
  "lib/components/flow/LatencyWaterfall.svelte",
  "lib/components/flow/WorkflowTimeline.svelte",
  "lib/components/primitives/TraceRow.svelte"
];
const HOVER_LAYOUT_PROPERTY = /\b(transform|(?:min-|max-)?height|padding(?:-[\w-]+)?|border-width)\s*:/g;

/* Literal times in styles that are a loop's own cycle, not a UI moment's duration. */
const DURATION_ALLOWED = [
  { file: "lib/components/chat/SessionControls.svelte", literal: "800ms", why: "the refresh button's spinner cycle" },
  { file: "lib/components/primitives/StateGlyph.svelte", literal: "1s", why: "the running glyph's spinner cycle" },
  { file: "lib/components/primitives/StatusChip.svelte", literal: "900ms", why: "a live status icon's breathing cycle" }
];
const TIME_LITERAL = /(?<![\w.-])(?:\d*\.)?\d+m?s\b/g;
const DECLARATION = /(?:^|[;{\s])(--[\w-]+|[a-z-]+)\s*:\s*([^;{}]+)/g;
const ANIMATES = (css, markup) =>
  /(?:^|[;{\s])(?:transition|animation)(?:-[a-z-]+)?\s*:(?!\s*none\b)/.test(css) || /@keyframes\b/.test(css) || /\s(?:in|out|transition|animate):\w+[\s=>/]/.test(markup);
/* Files that animate but whose guard lives elsewhere. */
const REDUCED_MOTION_INHERITED = [];
/* Blur is the costliest paint there is; only these few small, short-lived cards carry it. */
const BLUR_ALLOWED = ["lib/components/agent/PendingBannerStack.svelte"];

describe("console conventions", () => {
  it("scans the whole source tree", () => {
    assert.ok(sources.length > 50, `only ${sources.length} files found — is the scan root right?`);
  });

  it("lists every scanned source file in the Bazel sources target", async () => {
    /* Under Bazel this test sees only what //typescript/apps/ui:sources names, so a file left out
       of that list would skip every scan here without failing. Outside Bazel the tree is real. */
    const build = await readFile(new URL("../BUILD.bazel", import.meta.url), "utf8");
    const list = build.match(/name = "sources",[\s\S]*?deps = \[([\s\S]*?)\]/)?.[1] ?? "";
    const declared = [...list.matchAll(/"\/\/typescript\/apps\/ui\/src((?:\/[\w.-]+)*):([\w.-]+)"/g)].map(([, dir, name]) => `${dir.slice(1)}${dir ? "/" : ""}${name}`);
    const scanned = sources.map(({ file }) => file.replace(/\.(svelte|ts)$/, ""));
    assert.deepEqual(scanned.filter((file) => !declared.includes(file)), [], "source files missing from :sources in typescript/apps/ui/BUILD.bazel: add their targets");
  });

  it("renders {@html} only in components the XSS suite covers", () => {
    const users = new Set(sources.filter(({ text }) => text.includes("{@html")).map(({ file }) => file));
    const offenders = [...users].filter((file) => !HTML_ALLOWED.includes(file));
    assert.deepEqual(
      offenders,
      [],
      "{@html} outside the allowlist. Render text instead, or route it through MarkdownMessage/JsonReply; " +
        "if raw HTML is truly needed, add hostile cases to tests/reply-xss.test.mjs and the file to HTML_ALLOWED."
    );
    assert.deepEqual(HTML_ALLOWED.filter((file) => !users.has(file)), [], "stale HTML_ALLOWED entries: remove them");
  });

  it("draws agent and model prose only through the shared markdown paths", () => {
    const offenders = [];
    for (const { file, text } of sources.filter(({ file }) => file.endsWith(".svelte"))) {
      for (const [, attribute, expr] of markupOf(text).matchAll(MARKUP_EXPRESSION)) {
        if (/^[#/:@]/.test(expr.trim()) || !PROSE_FIELD.test(expr)) continue;
        if (attribute === "text=" || expr.includes("markdownToText(")) continue;
        offenders.push(`${file}: ${attribute ?? ""}{${expr.trim()}}`);
      }
    }
    assert.deepEqual(
      offenders,
      [],
      "prose drawn raw. Use <MarkdownMessage text={…} compact /> for a block, or markdownToText(…) for a " +
        "one-line preview, label or tooltip (both in MarkdownMessage.svelte)."
    );
  });

  it("draws every image through SafeImage, which loads only what imagePolicy allows", () => {
    const offenders = sources.filter(({ file, text }) => /<img\b/i.test(text) && file !== "lib/components/primitives/SafeImage.svelte").map(({ file }) => file);
    assert.deepEqual(offenders, [], "a raw <img>. Render <SafeImage src alt /> instead, so the source passes imagePolicy.");
  });

  it("builds every data-driven href through safeHref", () => {
    const offenders = [];
    const used = new Set();
    for (const { file, text } of sources) {
      for (const match of text.matchAll(HREF)) {
        const expr = (match[1] ?? match[2] ?? match[3]).trim();
        if (expr.includes("safeHref(")) continue;
        const allowed = HREF_ALLOWED.find((entry) => entry.file === file && entry.expr === expr);
        if (allowed) used.add(allowed);
        else offenders.push(`${file}: href from \`${expr}\``);
      }
    }
    assert.deepEqual(
      offenders,
      [],
      "hrefs built from data without safeHref. Pass the URL through safeHref/citationLinks (MarkdownMessage.svelte), " +
        "or, for a URL the console builds itself, add { file, expr, why } to HREF_ALLOWED."
    );
    assert.deepEqual(HREF_ALLOWED.filter((entry) => !used.has(entry)).map(({ file, expr }) => `${file}: ${expr}`), [], "stale HREF_ALLOWED entries: remove them");
  });

  it("imports only the TanStack packages chosen for the console", () => {
    const imported = new Map();
    for (const { file, text } of sources) {
      for (const [, pkg] of text.matchAll(TANSTACK_IMPORT)) imported.set(pkg, file);
    }
    const offenders = [...imported].filter(([pkg]) => !TANSTACK_ALLOWED.includes(pkg)).map(([pkg, file]) => `${file}: ${pkg}`);
    assert.deepEqual(
      offenders,
      [],
      "new TanStack package. Check its weight in a `vite build`, then add it to TANSTACK_ALLOWED in the same commit."
    );
    assert.deepEqual(TANSTACK_ALLOWED.filter((pkg) => !imported.has(pkg)), [], "stale TANSTACK_ALLOWED entries: remove them");
  });

  it("insets every card pane by the shared pane gutter", async () => {
    const css = await readFile(new URL("./app.css", `file://${root}/`), "utf8");
    assert.match(css, /--pane-inset: var\(--gutter-tight\);/, "app.css lost the --pane-inset token");
    for (const { file, selector } of INSET_PANES) {
      const text = sources.find((source) => source.file === file)?.text ?? "";
      assert.match(ruleOf(text, selector) ?? "", /padding: var\(--pane-inset\);/, `${file}: ${selector} is not inset by --pane-inset`);
      assert.doesNotMatch(ruleOf(text, `${selector}.empty`) ?? "", /padding/, `${file}: the empty state picks its own inset`);
    }
    /* The file tree is a scroller, so a ring drawn outward loses its sides to it. */
    const trees = sources.find((source) => source.file === "lib/components/agent/MountTrees.svelte").text;
    assert.match(ruleOf(trees, ".row:focus-visible") ?? "", /outline-offset: -2px;/);
  });

  /* A styled track takes layout width, so a scroller padded the same on both sides sat 8px from
     the pane's left edge and 18px from its right, and its content reflowed as the track came. */
  it("holds a pane scroller's track in its end inset", async () => {
    const css = await readFile(new URL("./app.css", `file://${root}/`), "utf8");
    assert.match(css, /--scrollbar-size: var\(--pane-inset\);/);
    assert.match(css, /::-webkit-scrollbar \{\s*width: var\(--scrollbar-size\);\s*height: var\(--scrollbar-size\);/);
    const offenders = [];
    let scrollers = 0;
    /* Panes only: a dialog's scrollers sit in its own frame, not against a pane's edges. */
    for (const { file, text } of sources.filter(({ file }) => file.endsWith(".svelte") && !file.endsWith("Dialog.svelte"))) {
      for (const { selector, body } of cssRules(styleOf(text))) {
        const inset = body.match(/\bpadding: [^;]*var\((--(?:pane|chat)-inset)\)/)?.[1];
        if (!inset || !/\boverflow(?:-y)?: auto;/.test(body)) continue;
        scrollers += 1;
        const endInset = new RegExp(`padding-inline-end: calc\\(var\\(${inset}\\) - var\\(--scrollbar-size\\)\\);`);
        if (!/scrollbar-gutter: stable;/.test(body) || !endInset.test(body)) offenders.push(`${file}: ${selector}`);
      }
    }
    assert.ok(scrollers >= 3, "the scan found the Logs, Tokens and chat scrollers");
    assert.deepEqual(offenders, [], "keep the gutter and take --scrollbar-size off the end padding");
    const source = (file) => sources.find((entry) => entry.file === file).text;
    assert.match(ruleOf(source("lib/components/agent/MountTrees.svelte"), ".mounts") ?? "", /scrollbar-gutter: stable;/);
    assert.match(ruleOf(source("lib/components/agent/FilesPanel.svelte"), ".trees") ?? "", /margin-inline-end: calc\(-1 \* var\(--scrollbar-size\)\);/);
  });

  /* One thumb, one track, one width. A per-component scrollbar-width or webkit
     rule either hid the bar or painted a different one. */
  it("paints every scrollbar from the one shared rule", async () => {
    const css = await readFile(new URL("./app.css", `file://${root}/`), "utf8");
    assert.equal([...css.matchAll(/::-webkit-scrollbar \{/g)].length, 1);
    assert.match(css, /--scrollbar-thumb: color-mix\(in srgb, var\(--text-1\) 12%, transparent\);/);
    assert.match(css, /--scrollbar-track: transparent;/);
    assert.match(
      css,
      /::-webkit-scrollbar-thumb \{\s*border: 0;\s*border-radius: var\(--radius-xs\);\s*background: var\(--scrollbar-thumb\);/
    );
    assert.match(css, /::-webkit-scrollbar-track,\s*::-webkit-scrollbar-corner \{\s*background: var\(--scrollbar-track\);/);
    assert.match(css, /scrollbar-width: thin;\s*scrollbar-color: var\(--scrollbar-thumb\) var\(--scrollbar-track\);/);
    assert.doesNotMatch(css, /scrollbar-width:\s*none/);
    const offenders = sources
      .filter(({ text }) => /scrollbar-width\s*:|scrollbar-color\s*:|::-webkit-scrollbar/.test(text))
      .map(({ file }) => file);
    assert.deepEqual(offenders, [], "scrollbar look lives in app.css");
  });

  it("leaves the pane header to name the pane", () => {
    const source = (file) => sources.find((entry) => entry.file === file)?.text ?? "";
    const labels = Object.fromEntries(
      [...source("lib/panes/registry.ts").matchAll(/(\w+): \{\s*kindLabel: "([^"]+)"/g)].map(([, kind, label]) => [kind, label])
    );
    const app = source("App.svelte");
    const imports = Object.fromEntries(
      [...app.matchAll(/import (\w+)(?:,\s*\{[^}]*\})? from "\$lib\/([^"]+)"/g)].map(([, name, path]) => [name, `lib/${path}`])
    );
    const bodies = [...app.matchAll(/pane\.kind === "(\w+)"\}([\s\S]*?)(?=\{:else|\{\/if\})/g)].map(([, kind, body]) => {
      const component = stripComments(body).match(/<([A-Z]\w*)/)?.[1];
      return { kind, file: imports[component] };
    });
    assert.deepEqual(
      bodies.filter(({ file }) => source(file)).map(({ kind }) => kind).sort(),
      Object.keys(labels).sort(),
      "every pane kind should map to a component file"
    );
    const offenders = [];
    for (const { kind, file } of bodies) {
      for (const [, text] of source(file).matchAll(/<h[1-6][^>]*>([^<{]+)<\/h[1-6]>/g)) {
        if (text.trim().toLowerCase().startsWith(labels[kind].toLowerCase())) offenders.push(`${file}: "${text.trim()}"`);
      }
    }
    assert.deepEqual(offenders, [], "a heading inside the pane repeats the PaneShell header's kind; drop it and keep any counts in the toolbar");
  });

  it("keeps shared recipes in their one home", () => {
    const offenders = [];
    for (const { file, text } of sources) {
      for (const { pattern, home } of RECIPES_WITH_A_HOME) {
        if (pattern.test(text)) offenders.push(`${file}: use ${home}`);
      }
    }
    assert.deepEqual(offenders, [], "a copy of a recipe that already has a shared home");
  });

  it("shows selection as a tint, never a side stroke", async () => {
    const styles = [
      { file: "app.css", css: await readFile(join(root, "app.css"), "utf8") },
      ...sources
        .filter(({ file }) => file.endsWith(".svelte"))
        .map(({ file, text }) => ({ file, css: [...text.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join("\n") }))
    ];
    const offenders = [];
    const used = new Set();
    for (const { file, css } of styles) {
      for (const { selector, body } of cssRules(css)) {
        const stroke =
          [...body.matchAll(SIDE_BORDER)].some(([, value]) => !DIVIDER.test(value.trim())) ||
          (/box-shadow/.test(body) && SIDE_SHADOW.test(body)) ||
          EDGE_BAR(body);
        if (!stroke) continue;
        const allowed = SIDE_STROKE_ALLOWED.find((entry) => entry.file === file && entry.selector === selector);
        if (allowed) used.add(allowed);
        else offenders.push(`${file}: ${selector}`);
      }
    }
    assert.deepEqual(
      offenders,
      [],
      "a one-sided stroke. Show selected/open/current/changed with `background: var(--selected-bg)` (app.css) and " +
        "keep --focus-ring for :focus-visible; if the line is structure (a rail, a quote), add { file, selector, why } to SIDE_STROKE_ALLOWED."
    );
    assert.deepEqual(SIDE_STROKE_ALLOWED.filter((entry) => !used.has(entry)).map(({ file, selector }) => `${file}: ${selector}`), [], "stale SIDE_STROKE_ALLOWED entries: remove them");
  });

  it("draws every search and filter field in a pane with the shared .field", async () => {
    const css = await readFile(join(root, "app.css"), "utf8");
    assert.match(ruleOf(css, ".field,\n.form-control") ?? "", /height: var\(--control-height\);[\s\S]*border: 1px solid var\(--border\);/, "app.css lost the .field box");
    assert.match(css, /\.field:has\(> :is\(input, select\):focus-visible\),\s*\.form-control:focus-visible \{\s*outline: 2px solid var\(--focus-ring\);/, "app.css lost the .field focus ring");
    const offenders = [];
    const used = new Set();
    for (const { file, text } of sources.filter(({ file }) => file.endsWith(".svelte"))) {
      const markup = stripComments(markupOf(text));
      const controls = [...markup.matchAll(FIELD_CONTROL)].filter(([, tag, attrs]) => isFieldControl(tag, attrs));
      if (controls.length === 0) continue;
      const allowed = FIELD_ALLOWED.find((entry) => entry.file === file);
      if (allowed) {
        used.add(allowed);
        continue;
      }
      for (const { 1: tag, index } of controls) {
        const before = markup.slice(0, index);
        const open = before.lastIndexOf('class="field"');
        if (open === -1 || /<\/(?:label|div)>/.test(before.slice(open))) offenders.push(`${file}: a <${tag}> outside a .field`);
      }
      const style = [...text.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join("\n");
      for (const { selector, body } of cssRules(style)) {
        if (/(?:^|[\s>,])(?:input|select)\b/.test(selector) && OWN_BOX.test(body)) offenders.push(`${file}: ${selector} draws its own box`);
      }
    }
    assert.deepEqual(
      offenders,
      [],
      'a search or filter field with its own box. Wrap the input or select in <label class="field"> (app.css) with its ' +
        "icon or word before it, and leave its border, height and ring to .field; or add { file, why } to FIELD_ALLOWED."
    );
    assert.deepEqual(FIELD_ALLOWED.filter((entry) => !used.has(entry)).map(({ file }) => file), [], "stale FIELD_ALLOWED entries: remove them");
  });

  it("draws every dropdown with the shared Select", () => {
    /* Each screen wrote its own <select>: the browser's caret and sans value in a form, a
       hand-rolled lead in the drawer, and a required field that drew a choice it did not hold. */
    const offenders = sources
      .filter(({ file, text }) => file !== "lib/components/primitives/Select.svelte" && /<select\b/.test(stripComments(markupOf(text))))
      .map(({ file }) => file);
    assert.deepEqual(offenders, [], "a raw <select>. Use $lib/components/primitives/Select.svelte, which owns the box, chevron and placeholder.");
  });

  it("draws every selected, open and current row with the one shared tint", async () => {
    assert.match(await readFile(join(root, "app.css"), "utf8"), /\n\s*--selected-bg: /, "app.css lost the --selected-bg token");
    const source = (file) => sources.find((entry) => entry.file === file)?.text ?? "";
    for (const [file, selector] of [
      ["lib/components/agent/ApprovalDecisionPanel.svelte", ".evaluation.chosen"],
      ["lib/components/chat/SessionControls.svelte", ".session-row.active"],
      ["lib/components/chat/StartSessionDialog.svelte", ".agent-card.selected"],
      ["lib/components/agent/TranscriptPanel.svelte", ".log-line.active-row"],
      ["lib/components/agent/TranscriptPanel.svelte", ".turn-group.active-turn .turn-head"],
      ["lib/components/agent/AgentStatePanel.svelte", ".change"],
      ["lib/components/agent/MountTrees.svelte", ".row.selected"]
    ]) {
      assert.match(ruleOf(source(file), selector) ?? "", /var\(--selected-bg\)/, `${file}: ${selector} should use var(--selected-bg)`);
    }
  });

  it("draws Logs turns as flat sections, not cards", () => {
    /* A hairline box reads as "press me", and a turn is not pressed as a whole. */
    const style = styleOf(sources.find(({ file }) => file === "lib/components/agent/TranscriptPanel.svelte")?.text ?? "");
    const groupRules = cssRules(style).filter(({ selector }) => /\.turn-group\b/.test(selector) && !/\.turn-group[^,]*\s/.test(selector));
    assert.ok(groupRules.length > 0, "TranscriptPanel lost its .turn-group rule");
    for (const { selector, body } of groupRules) {
      assert.doesNotMatch(body, /\b(?:border(?:-(?:top|left|right|inline|inline-start|inline-end|block-start))?|border-radius|background(?:-color)?|box-shadow)\s*:/, `${selector} draws a card; rule turns off with border-bottom only`);
    }
  });

  it("names the properties a transition animates", async () => {
    const offenders = (await motionSources()).filter(({ text }) => /\btransition(?:-property)?\s*:\s*all\b/.test(text)).map(({ file }) => file);
    assert.deepEqual(offenders, [], "`transition: all`. List the properties that change, usually opacity and transform.");
  });

  it("keeps hover from moving or resizing rows", async () => {
    const styles = new Map([
      ["app.css", await readFile(join(root, "app.css"), "utf8")],
      ...sources.filter(({ file }) => file.endsWith(".svelte")).map(({ file, text }) => [file, styleOf(text)])
    ]);
    const offenders = [];
    for (const file of HOVER_STILL_FILES) {
      for (const { selector, body } of cssRules(styles.get(file) ?? "")) {
        if (!selector.includes(":hover")) continue;
        for (const [, property] of body.matchAll(HOVER_LAYOUT_PROPERTY)) offenders.push(`${file}: ${selector} sets ${property}`);
      }
    }
    assert.deepEqual(offenders, [], "hover must only change paint; it must not move or resize a row");
    const stale = HOVER_STILL_FILES.filter((file) => !cssRules(styles.get(file) ?? "").some(({ selector }) => selector.includes(":hover")));
    assert.deepEqual(stale, [], "stale HOVER_STILL_FILES entries (gone, or no :hover rule left): remove them");
  });

  it("times motion with the app.css duration and easing tokens", async () => {
    const offenders = [];
    const used = new Set();
    for (const { file, text, css } of await motionSources()) {
      for (const [, property, value] of (css ? text : styleOf(text)).replace(/\/\*[\s\S]*?\*\//g, "").matchAll(DECLARATION)) {
        if (file === "app.css" && property.startsWith("--")) continue;
        if (/cubic-bezier\(/.test(value)) offenders.push(`${file}: ${property}: ${value.trim()}`);
        for (const [literal] of value.matchAll(TIME_LITERAL)) {
          if (/^0m?s$/.test(literal)) continue;
          const allowed = DURATION_ALLOWED.find((entry) => entry.file === file && entry.literal === literal);
          if (allowed) used.add(allowed);
          else offenders.push(`${file}: ${property}: ${value.trim()}`);
        }
      }
    }
    assert.deepEqual(
      offenders,
      [],
      "a literal duration or curve. Use a --duration-* token from app.css (press to close, fast or pane to open, settle for the queue) and an --ease-* token; " +
        "a spinner or loader's own cycle goes in DURATION_ALLOWED with a why."
    );
    assert.deepEqual(DURATION_ALLOWED.filter((entry) => !used.has(entry)).map(({ file, literal }) => `${file}: ${literal}`), [], "stale DURATION_ALLOWED entries: remove them");
  });

  it("gives everything that moves a reduced-motion path", async () => {
    const animating = (await motionSources()).filter(({ text, css }) =>
      ANIMATES(stripComments((css ? text : styleOf(text)).replace(/\/\*[\s\S]*?\*\//g, "")), css ? "" : stripComments(markupOf(text)))
    );
    const offenders = animating
      .filter(({ file, text }) => !/prefers-reduced-motion|prefersReducedMotion/.test(text) && !REDUCED_MOTION_INHERITED.some((entry) => entry.file === file))
      .map(({ file }) => file);
    assert.deepEqual(
      offenders,
      [],
      "animates with no reduced-motion path. Add `@media (prefers-reduced-motion: reduce)` (or read prefersReducedMotion " +
        "from svelte/motion); if the guard lives in a shared class, add { file, why } to REDUCED_MOTION_INHERITED."
    );
    const files = new Set(animating.map(({ file }) => file));
    assert.deepEqual(REDUCED_MOTION_INHERITED.filter((entry) => !files.has(entry.file)).map(({ file }) => file), [], "stale REDUCED_MOTION_INHERITED entries: remove them");
  });

  it("blurs only the attention queue's cards", async () => {
    const blurred = (await motionSources()).filter(({ text }) => /\bblur\(/.test(text)).map(({ file }) => file);
    assert.deepEqual(blurred.filter((file) => !BLUR_ALLOWED.includes(file)), [], "a blur. Fade and move instead; never blur a pane, dialog, backdrop or row.");
    assert.deepEqual(BLUR_ALLOWED.filter((file) => !blurred.includes(file)), [], "stale BLUR_ALLOWED entries: remove them");
  });

  it("resizes only non-windowed cards with the shared transition", async () => {
    const source = (file) => sources.find((entry) => entry.file === file)?.text ?? "";
    const css = await readFile(join(root, "app.css"), "utf8");
    assert.match(css, /\.animate-size \{\s*interpolate-size: allow-keywords;\s*transition:\s*width var\(--duration-pane\) var\(--ease-out\),\s*height var\(--duration-pane\) var\(--ease-out\);\s*\}/);
    assert.match(css, /@media \(prefers-reduced-motion: reduce\) \{\s*\.animate-size \{\s*transition: none !important;/);
    const queue = source("lib/components/agent/PendingBannerStack.svelte");
    assert.match(queue, /class="viewport animate-size"/);
    assert.match(queue, /height = viewport\?\.offsetHeight \?\? height;\s*paging = true;/, "paging holds the old card height before changing rows");
    assert.match(source("lib/components/primitives/LongContent.svelte"), /ready && "animate-size"/);
    assert.match(source("lib/components/chat/JsonReply.svelte"), /<LongContent/);
    assert.doesNotMatch(source("lib/components/chat/JsonReply.svelte"), /json-toggle|FOLD_AFTER_LINES/);
    assert.doesNotMatch(source("lib/components/primitives/TraceRow.svelte"), /animate-size/);
    assert.doesNotMatch(source("lib/components/primitives/WindowedRows.svelte"), /animate-size/);
  });
});
