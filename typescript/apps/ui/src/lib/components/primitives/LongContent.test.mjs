// ABOUTME: Pins the shared long-content clamp's threshold, streaming, accessibility, memory,
// full-DOM and virtual-list contracts without replacing its rendered-height browser measurement.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { exceedsLongContentThreshold, mayCollapse } from "./LongContent.svelte";

const source = readFileSync(new URL("./LongContent.svelte", import.meta.url), "utf8");
const appCss = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
const chat = readFileSync(new URL("../agent/AgentChatPanel.svelte", import.meta.url), "utf8");
const logs = readFileSync(new URL("../agent/TranscriptPanel.svelte", import.meta.url), "utf8");

describe("the shared long-content clamp", () => {
  it("collapses only beyond the rendered-height threshold", () => {
    assert.equal(exceedsLongContentThreshold(800, 800), false);
    assert.equal(exceedsLongContentThreshold(802, 800), true);
  });

  it("never collapses streaming content", () => {
    assert.equal(mayCollapse(true, 2_000, 800), false);
    assert.equal(mayCollapse(false, 2_000, 800), true);
  });

  it("keeps content mounted and copies full source values", () => {
    assert.match(source, /\{@render children\(\)\}/);
    assert.doesNotMatch(source, /slice\(|substring\(|headLines/);
    assert.match(chat, /<Copyable value=\{message\.text\}/);
    assert.match(logs, /<Copyable block value=\{primary\.text\}/);
  });

  it("connects an accessible quiet toggle to its region", () => {
    assert.match(source, /aria-expanded=\{expanded\} aria-controls=\{regionId\}/);
    assert.match(source, /tabindex=\{expanded \? 0 : undefined\}/);
    assert.match(source, /\.long-content-toggle \{[\s\S]*?border: 0;/);
  });

  /* Pulled up over the fade, the toggle hid an open block's last line: a log's timestamp. */
  it("covers only the collapsed fade, never an open block's last line", () => {
    assert.match(source, /\.long-content-toggle \{[\s\S]*?margin-top: calc\(-1 \* /);
    assert.match(source, /\.expanded \.long-content-toggle \{\s*margin-top: 0;\s*\}/);
  });

  it("remembers state by content id and asks both virtual lists to remeasure", () => {
    assert.match(source, /remembered\[id\] = expanded/);
    assert.match(chat, /const remeasureMessages = \(\) => messageList\(\)\.measure\(\)/);
    assert.match(logs, /rowLists\[turnNumber\]\?\.measure\(\);[\s\S]*turnList\(\)\.measure\(\)/);
  });

  it("uses shared threshold and motion tokens with an instant reduced-motion path", () => {
    assert.match(appCss, /--long-content-max-lines: 40;/);
    assert.match(appCss, /--long-content-max-height: calc\(var\(--long-content-max-lines\) \* 1lh\);/);
    assert.match(source, /ready && "animate-size"/);
    assert.match(source, /@media \(prefers-reduced-motion: reduce\)[\s\S]*transition: none !important;/);
  });
});
