// ABOUTME: Pins how a reply's markdown table renders: emphasis inside a cell is parsed, an
// escaped pipe stays inside its cell instead of splitting a bold run across two, and cells do not
// inherit the message's `overflow-wrap: anywhere`, which lets auto table layout squeeze a column
// until "**Denver" wraps a few letters per line.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import MarkdownMessage from "./MarkdownMessage.svelte";

const cellsOf = (text) =>
  [...render(MarkdownMessage, { props: { text } }).body.matchAll(/<td>(.*?)<\/td>/g)].map(
    (match) => match[1]
  );

describe("markdown tables in a reply", () => {
  it("parses bold inside a cell", () => {
    const cells = cellsOf("| Origin | Fare |\n|---|---|\n| **Denver (DEN)** | $312 |");
    assert.deepEqual(cells, ["<strong>Denver (DEN)</strong>", "$312"]);
  });

  it("keeps an escaped pipe inside its cell", () => {
    const cells = cellsOf("| Route | Fare |\n|---|---|\n| **DEN \\| COS** | $312 |");
    assert.deepEqual(cells, ["<strong>DEN | COS</strong>", "$312"]);
  });

  it("lets a cell wrap only between words unless a word cannot fit", () => {
    const source = readFileSync(new URL("./MarkdownMessage.svelte", import.meta.url), "utf8");
    const cellRule = source.match(/:global\(th\),\s*\.markdown-message :global\(td\) \{[^}]*\}/);
    assert.ok(cellRule, "the shared th/td rule is where the cell's wrapping is set");
    assert.match(cellRule[0], /overflow-wrap: break-word/);
  });
});
