// Pins the graph's details dialog to the shared parts. Its header was a decorative tone square,
// a 24px title and a boxed status badge beside a boxed close button, unlike every card it opens
// from; the reply and payload had no copy control; and JSON was a raw <pre> with no
// highlighting. No SSR render can open a <dialog> or reach SvelteFlow's context, so this reads
// the markup.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";
import { stripComments } from "./support/controllerHarness.mjs";

const flow = stripComments(
  readFileSync(new URL("../src/lib/components/flow/AgentStateFlow.svelte", import.meta.url), "utf8")
);
const dialog = flow.slice(flow.indexOf("<dialog"), flow.indexOf("</dialog>"));
const head = dialog.slice(dialog.indexOf("<header"), dialog.indexOf("</header>"));
const sections = dialog.slice(dialog.indexOf("{#each sections"), dialog.indexOf("{/each}"));

describe("the graph details dialog", () => {
  it("heads with the shared row: glyph, title, plain status word, quiet close", () => {
    assert.match(head, /<header class="surface-head">/);
    assert.match(head, /<h2 class="head-name">\s*<StateGlyph [^>]*\/>\s*<span class="head-title"/);
    assert.match(head, /<StatusChip [^>]*\bplain\b/);
    assert.match(head, /<button[^>]*class="head-button"[^>]*aria-label="Close details"/);
    assert.doesNotMatch(head, /<IconButton|<Badge/, "nothing boxed in the head");
    assert.doesNotMatch(flow, /inspector-dot/, "no decorative tone square");
  });

  it("puts every section's block in a Copyable block under a kicker", () => {
    assert.match(sections, /<h3 class="kicker">\{section\.label\}<\/h3>/);
    assert.match(sections, /<Copyable block value=\{section\.text\}/);
    const copyable = sections.slice(sections.indexOf("<Copyable"), sections.indexOf("</Copyable>"));
    for (const block of ["<MarkdownMessage", "<pre"]) assert.ok(copyable.includes(block), `${block} sits inside the Copyable`);
  });

  it("draws JSON through the shared highlighter, left to right and keyboard-scrollable", () => {
    assert.match(flow, /import \{ highlight \} from "\$lib\/highlight";/);
    const code = sections.slice(sections.indexOf('class="expanded-detail code"') - 20);
    assert.match(code, /role="region"\s+aria-label=\{section\.label\}\s+tabindex="0"/);
    assert.match(code, /<code dir="ltr">\{@html highlight\(section\.text, section\.kind === "json" \? "json"/);
    assert.doesNotMatch(sections, /<code>\{section\.text\}<\/code>/, "no raw JSON pre");
  });
});
