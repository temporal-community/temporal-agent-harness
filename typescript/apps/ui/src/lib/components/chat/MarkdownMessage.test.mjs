// Exercise the real SSR component so list structure and inline table formatting stay visible
// to callers, independent of the renderer's parsing implementation.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import MarkdownMessage from "./MarkdownMessage.svelte";

const cellsOf = (text) =>
  [...render(MarkdownMessage, { props: { text } }).body.matchAll(/<td>(.*?)<\/td>/g)].map(
    (match) => match[1]
  );

const renderReply = (text) => render(MarkdownMessage, { props: { text } }).body;

// Extract only the semantic list tree. Ignore inline wrappers and whitespace so these checks
// tolerate paragraph wrappers in loose lists without accepting detached or flattened sublists.
function listsOf(html) {
  const roots = [];
  const stack = [];
  for (const token of html.match(/<[^>]*>|[^<]+/g) ?? []) {
    const list = token.match(/^<(ol|ul)\b([^>]*)>/);
    if (list) {
      const node = { type: list[1], items: [] };
      if (node.type === "ol") node.start = Number(list[2].match(/\bstart="(\d+)"/)?.[1] ?? 1);
      const parent = stack.at(-1);
      (parent?.lists ?? roots).push(node);
      stack.push(node);
    } else if (/^<li(?:\s[^>]*)?>/.test(token)) {
      const item = { text: "", lists: [] };
      assert.ok(stack.at(-1)?.items, "list items must belong to a list");
      stack.at(-1).items.push(item);
      stack.push(item);
    } else if (/^<\/(?:ol|ul|li)>/.test(token)) {
      stack.pop();
    } else if (!token.startsWith("<") && stack.at(-1)?.text !== undefined) {
      stack.at(-1).text += token;
    } else if (/^<\/?p\b|^<br\b/.test(token) && stack.at(-1)?.text !== undefined) {
      stack.at(-1).text += " ";
    }
  }
  assert.equal(stack.length, 0, "all list elements must close");
  const normalize = (list) => {
    for (const item of list.items) {
      item.text = item.text.replace(/\s+/g, " ").trim();
      item.lists.forEach(normalize);
    }
  };
  roots.forEach(normalize);
  return roots;
}

const item = (text, lists = []) => ({ text, lists });
const ordered = (items, start = 1) => ({ type: "ol", items, start });
const unordered = (items) => ({ type: "ul", items });

const toolListReply = `In this chat, I currently have these tools:

1. **Ask a follow-up question**
   - Used when I need information or a decision from you.
   - Can offer a list of choices, including multiple-choice selection.

2. **MAC-address vendor lookup**
   - Input: a complete MAC address, in formats such as:
     - \`FC:A1:3E:2A:1C:33\`
     - \`FC-A1-3E-2A-1C-33\`
     - \`FCA1.3E2A.1C33\`
     - \`FCA13E2A1C33\`
   - Output: the registered hardware vendor, or no result if unavailable.
   - It queries MACVendors.com and requires your approval for each lookup.

3. **Short Python orchestration**
   - I can run a small, sandboxed Python script to coordinate available tools, process their results, or make several independent tool calls efficiently.
   - It has no direct internet, filesystem, environment, or arbitrary-package access. It can only call tools that are available in the chat.

4. **Parallel tool execution**
   - I can run independent available tool calls at the same time when that makes sense.

Tool availability may differ between chats, and I’ll tell you or ask permission when a tool action is needed.`;

describe("markdown lists in a reply", () => {
  it("keeps numbered siblings in one list when an item has nested bullets", () => {
    assert.deepEqual(listsOf(renderReply("1. First\n   - Detail\n2. Second")), [
      ordered([item("First", [unordered([item("Detail")])]), item("Second")])
    ]);
  });

  it("keeps blank-separated numbered items in one list", () => {
    assert.deepEqual(listsOf(renderReply("1. First\n\n2. Second\n\n3. Third")), [
      ordered([item("First"), item("Second"), item("Third")])
    ]);
  });

  it("retains the first ordered marker as the list's start", () => {
    assert.deepEqual(listsOf(renderReply("7. Seventh\n   - Detail\n\n8. Eighth")), [
      ordered([item("Seventh", [unordered([item("Detail")])]), item("Eighth")], 7)
    ]);
  });

  it("nests ordered and unordered lists without promoting their items", () => {
    const text = "- Outer\n  3. Third\n     - Deep detail\n  4. Fourth\n- Last";
    assert.deepEqual(listsOf(renderReply(text)), [
      unordered([
        item("Outer", [ordered([
          item("Third", [unordered([item("Deep detail")])]),
          item("Fourth")
        ], 3)]),
        item("Last")
      ])
    ]);
  });

  it("keeps indented continuation paragraphs inside their item and ends at an outside paragraph", () => {
    const text = "1. First\n   continued text\n\n   Another paragraph.\n\n2. Second\n\nOutside paragraph.";
    const html = renderReply(text);
    assert.deepEqual(listsOf(html), [
      ordered([item("First continued text Another paragraph."), item("Second")])
    ]);
    assert.match(html, /<\/ol>\s*<p>Outside paragraph\.<\/p>/);
  });

  it("ends a list before a heading and keeps a later numbered list independent", () => {
    const html = renderReply("1. First\n\n## Next section\n\n3. Third");
    assert.deepEqual(listsOf(html), [ordered([item("First")]), ordered([item("Third")], 3)]);
    assert.match(html, /<\/ol>\s*<h4>Next section<\/h4>\s*<ol\b/);
  });

  it("preserves citation links inside a nested blockquote", () => {
    const html = render(MarkdownMessage, { props: {
      text: "1. Parent\n   > Quoted [1]\n2. Next",
      citations: [{ file_name: "Reference", document_uri: "https://example.com/reference" }]
    } }).body;
    const quote = html.match(/<blockquote>([\s\S]*?)<\/blockquote>/)?.[1];
    assert.ok(quote, "the indented quote belongs to the list item");
    assert.match(quote, /class="md-citation"/);
    assert.match(quote, /href="https:\/\/example\.com\/reference"/);
    assert.equal((html.match(/class="md-citation"/g) ?? []).length, 1);
  });

  it("preserves the complete reported tool reply as four numbered items with nested details", () => {
    const html = renderReply(toolListReply);
    assert.deepEqual(listsOf(html), [ordered([
      item("Ask a follow-up question", [unordered([
        item("Used when I need information or a decision from you."),
        item("Can offer a list of choices, including multiple-choice selection.")
      ])]),
      item("MAC-address vendor lookup", [unordered([
        item("Input: a complete MAC address, in formats such as:", [unordered([
          item("FC:A1:3E:2A:1C:33"),
          item("FC-A1-3E-2A-1C-33"),
          item("FCA1.3E2A.1C33"),
          item("FCA13E2A1C33")
        ])]),
        item("Output: the registered hardware vendor, or no result if unavailable."),
        item("It queries MACVendors.com and requires your approval for each lookup.")
      ])]),
      item("Short Python orchestration", [unordered([
        item("I can run a small, sandboxed Python script to coordinate available tools, process their results, or make several independent tool calls efficiently."),
        item("It has no direct internet, filesystem, environment, or arbitrary-package access. It can only call tools that are available in the chat.")
      ])]),
      item("Parallel tool execution", [unordered([
        item("I can run independent available tool calls at the same time when that makes sense.")
      ])])
    ])]);
    assert.match(html, /<strong>MAC-address vendor lookup<\/strong>/);
    assert.match(html, /<code>FC:A1:3E:2A:1C:33<\/code>/);
    assert.match(html, /<\/ol>\s*<p>Tool availability may differ between chats,/);
  });
});

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
