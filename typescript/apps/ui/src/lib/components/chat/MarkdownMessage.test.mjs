// ABOUTME: Pins how a reply's markdown renders through TanStack Markdown. Tables: emphasis inside a
// cell is parsed, an escaped pipe stays inside its cell, and cells do not inherit the message's
// `overflow-wrap: anywhere`. Streaming: a chunk cut anywhere still renders; an append changes only
// the last block, so the blocks above keep their nodes; only the live block has its unfinished
// markup closed; an open fence is drawn plain until it closes. No line of the reply is dropped.
// Hostile input runs in a child process that is killed if it hangs, so a hang fails, not freezes.
// Links: only a normalized http(s), mailto or same-origin URL becomes an anchor. Images: only one
// imagePolicy allows becomes a placeholder for SafeImage; any other is its alt text and never loads.
// markdownToText reads markdown as the plain text a preview shows (an image as its alt), and other
// surfaces draw their prose through this same renderer.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { render } from "svelte/server";
import { beforeAll, describe, it } from "vitest";

import { emptyValues, resultForm } from "@temporalio/agent-harness-client";
import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import CallInput from "../agent/CallInput.svelte";
import MarkdownMessage, { citationAriaLabel, citationLinks, markdownBlocks, markdownToText, renderBlock } from "./MarkdownMessage.svelte";
import SchemaForm from "./SchemaForm.svelte";
import StartSessionDialog from "./StartSessionDialog.svelte";

const markup = (props) => stripComments(render(MarkdownMessage, { props }).body);
const cellsOf = (text) => [...markup({ text }).matchAll(/<td>(.*?)<\/td>/g)].map((match) => match[1]);

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

describe("a reply cut off mid-chunk", () => {
  /* A bare list marker once looped forever: the block scan accepted the line, the list
     renderer took nothing, and the scan never moved. */
  const cuts = ["- ", "* ", "1. ", "1.", "#", ">", "|", "`", "```", "~~~", "[", "![", "Intro\n\n- first\n- ", "| a |\n|", "> ```"];
  for (const text of cuts) {
    it(`renders ${JSON.stringify(text)}`, () => {
      for (const streaming of [false, true]) {
        const body = markup({ text, streaming });
        assert.match(body, /<div class="markdown-message/);
        assert.doesNotMatch(body, /undefined|null/);
      }
    });
  }

  it("draws an empty item rather than nothing, streaming or not", () => {
    for (const streaming of [false, true]) {
      assert.match(markup({ text: "- ", streaming }), /<ul>\s*<li><\/li>\s*<\/ul>/);
      assert.match(markup({ text: "#", streaming }), /<h3><\/h3>/);
      assert.match(markup({ text: ">", streaming }), /<blockquote>\s*<\/blockquote>/);
    }
  });
});

/* A parser that loops forever blocks the thread its test runs on, so the test's own timeout never
   fires. These cases run in a child process, killed if it overruns, so a hang fails the run. */
const CHILD = `
  import { tmpdir } from "node:os";
  import { createServer } from "vite";
  const server = await createServer({
    configFile: "vitest.config.ts", cacheDir: tmpdir() + "/markdown-hostile-vite-" + process.pid, logLevel: "silent", appType: "custom",
    server: { middlewareMode: true, hmr: false, ws: false }, optimizeDeps: { noDiscovery: true }
  });
  const { markdownBlocks, renderBlock } = await server.ssrLoadModule("/src/lib/components/chat/MarkdownMessage.svelte");
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  for (const [name, text] of JSON.parse(input)) {
    console.log(JSON.stringify({ name }));
    let ms = 0;
    let rendered;
    /* Only the second, warm pass is timed: the first is mostly the JIT. */
    for (const streaming of [false, true]) {
      const started = performance.now();
      const blocks = markdownBlocks(text, []);
      const drawn = blocks.map((block, index) => renderBlock(block, [], streaming && index === blocks.length - 1));
      if (streaming) ms = performance.now() - started;
      rendered ??= drawn;
    }
    console.log(JSON.stringify({ name, ms, rendered }));
  }
  await server.close();
`;

function runIsolated(cases) {
  const cwd = fileURLToPath(new URL("../../../../", import.meta.url));
  const run = spawnSync(process.execPath, ["--input-type=module", "-e", CHILD], { cwd, input: JSON.stringify(cases), encoding: "utf8", timeout: 25_000 });
  const lines = run.stdout.split("\n").filter((line) => line.startsWith("{")).map((line) => JSON.parse(line));
  const hung = run.error || run.signal || run.status ? (lines.at(-1)?.name ?? "startup") : null;
  return { hung, stderr: run.stderr, results: new Map(lines.filter((line) => "ms" in line).map((line) => [line.name, line])) };
}

describe("hostile input, in a child process", () => {
  const cases = {
    "2,000 nested quotes": ">".repeat(2000),
    "50,000 brackets": "[".repeat(50_000),
    /* Each level re-slices the lines below it, so depth costs about quadratically: 1,000+ deep
       lists take hundreds of ms. Accepted as slow; this guards the depth a reply might reach. */
    "200-deep list": Array.from({ length: 200 }, (_, depth) => `${"  ".repeat(depth)}- x`).join("\n"),
    /* TanStack's line patterns stop at U+2028/U+2029: a quote holding one looped forever. */
    "quote U+2028": "> hello\u2028world",
    "quote U+2029 mid reply": "Intro\n\n> quoted\u2029text\n\nmore",
    "list item U+2028": "- a\u2028b\n- c",
    "heading U+2028": "# Title\u2028more",
    "fence opener U+2028": "```js\u2028\ncode\n```\n\nafter",
    "table cell U+2028": "| a | b |\n|---|---|\n| x\u2028y | z |",
    "fenced code U+2028": '```json\n{"a": "x\u2028y"}\n```'
  };
  let run;
  beforeAll(() => {
    run = runIsolated(Object.entries(cases));
  });
  const drawn = (name) => {
    assert.equal(run.hung, null, `hung or crashed on ${run.hung}\n${run.stderr}`);
    return run.results.get(name);
  };

  for (const [name, budget] of [["2,000 nested quotes", 500], ["50,000 brackets", 500], ["200-deep list", 250]]) {
    it(`renders ${name} within ${budget} ms`, () => {
      const { ms } = drawn(name);
      assert.ok(ms < budget, `${Math.round(ms)} ms`);
    });
  }

  it("keeps U+2028 and U+2029 inside the quote, item, heading and cell they were typed in", () => {
    assert.deepEqual(drawn("quote U+2028").rendered, [{ kind: "html", html: "<blockquote>\n<p>hello\u2028world</p>\n</blockquote>" }]);
    assert.deepEqual(drawn("quote U+2029 mid reply").rendered.map((block) => block.html), ["<p>Intro</p>", "<blockquote>\n<p>quoted\u2029text</p>\n</blockquote>", "<p>more</p>"]);
    assert.equal(drawn("list item U+2028").rendered[0].html, "<ul>\n<li>a\u2028b</li>\n<li>c</li>\n</ul>");
    assert.equal(drawn("heading U+2028").rendered[0].html, "<h3>Title\u2028more</h3>");
    assert.match(drawn("table cell U+2028").rendered[0].html, /<tbody><tr><td>x\u2028y<\/td><td>z<\/td><\/tr><\/tbody>/);
  });

  it("opens a fence whose info string holds U+2028, and copies U+2028 in code exactly", () => {
    const [fence, after] = drawn("fence opener U+2028").rendered;
    assert.deepEqual(fence, { kind: "code", code: "code", language: "js", open: false });
    assert.equal(after.html, "<p>after</p>");
    assert.equal(drawn("fenced code U+2028").rendered[0].code, '{"a": "x\u2028y"}');
  });
});

/* A reply is drawn block by block, so a list split across two blocks would restart at 1. */
describe("numbered lists in a reply", () => {
  const text = "1. Plan\n   - detail\n\n2. Build\n\n3. Ship";

  it("keeps one list across nested bullets and blank lines, at every point while it streams", () => {
    for (let end = 1; end <= text.length; end += 1) {
      const html = markup({ text: text.slice(0, end), streaming: end < text.length });
      assert.ok((html.match(/<ol\b/g)?.length ?? 0) <= 1, JSON.stringify(text.slice(0, end)));
    }
    assert.match(markup({ text }), /<ol>[\s\S]*Plan[\s\S]*<ul>\s*<li>detail<\/li>\s*<\/ul>[\s\S]*Build[\s\S]*Ship[\s\S]*<\/ol>/);
  });

  it("keeps the first number as the list's start", () => {
    assert.match(markup({ text: "7. Seventh\n   - detail\n\n8. Eighth" }), /<ol start="7">/);
  });
});

describe("TanStack Markdown syntax", () => {
  /* Accepted differences from CommonMark: entities draw as typed, `~~` is <del>. */
  it("draws strikethrough, h5 and h6, nested lists, and entities as typed", () => {
    assert.match(markup({ text: "~~gone~~" }), /<del>gone<\/del>/);
    assert.match(markup({ text: "##### five\n\n###### six" }), /<h6>five<\/h6>[\s\S]*<h6>six<\/h6>/);
    assert.match(markup({ text: "- outer\n  - inner" }), /<li>outer\s*<ul>\s*<li>inner<\/li>/);
    assert.match(markup({ text: "a &lt;b&gt; &amp; c" }), /<p>a &amp;lt;b&amp;gt; &amp;amp; c<\/p>/);
  });
});

describe("the reply's text", () => {
  it("keeps every line, link and footnote definitions included, as text", () => {
    const text = "Claim[^1] and [docs][d].\n\n[^1]: The source text.\n[d]: /docs/d\n\nSee below\n[1]: /sources/1\n\nSources:\n\n[2]: /sources/2";
    const body = markup({ text });
    for (const line of text.split("\n").filter(Boolean)) assert.ok(body.includes(line), line);
    assert.doesNotMatch(body, /\\\[/);
    assert.match(markup({ text: "See below\n[1]: https://x" }), /<p>See below\n\[1\]: <a class="md-link" href="https:\/\/x\/"/);
  });

  it("leaves a definition-looking line inside a fence as it was typed", () => {
    const [block] = markdownBlocks("```\n[x]: /in/a/fence\n```", []);
    assert.equal(renderBlock(block, [], false).code, "[x]: /in/a/fence");
  });

  it("shows a footnote's text at every point of the stream", () => {
    const text = "Claim[^1].\n\n[^1]: The source text.";
    const start = text.indexOf("The");
    for (let end = start + 1; end <= text.length; end += 1) {
      assert.ok(markup({ text: text.slice(0, end), streaming: true }).includes(text.slice(start, end).trimEnd()), JSON.stringify(text.slice(0, end)));
    }
  });
});

describe("a streaming reply, block by block", () => {
  const doc = "Intro with **bold**.\n\n```ts\nconst a = 1;\n```\n\n- one\n- two\n\n## Heading\n\nTail";

  it("changes only the last block when text is appended", () => {
    const before = markdownBlocks(doc, []);
    const after = markdownBlocks(`${doc} grows`, []);
    assert.equal(before.length, 5);
    assert.deepEqual(after.slice(0, -1), before.slice(0, -1));
    assert.notEqual(after.at(-1), before.at(-1));
  });

  /* What the keyed `{#each}` re-renders: a block whose source string changed. */
  it("re-renders a number of blocks linear in the chunks, not quadratic", () => {
    const long = Array.from({ length: 20 }, (_, i) => `## Part ${i}\n\nSome *prose* ${i}.\n\n\`\`\`py\nx = ${i}\n\`\`\`\n`).join("\n");
    let previous = [];
    let rendered = 0;
    let chunks = 0;
    for (let end = 40; end < long.length + 40; end += 40) {
      const blocks = markdownBlocks(long.slice(0, end), []);
      rendered += blocks.filter((block, index) => block !== previous[index]).length;
      previous = blocks;
      chunks += 1;
    }
    assert.ok(rendered <= chunks * 2 + previous.length, `${rendered} block renders for ${chunks} chunks`);
  });

  it("never changes a block above the live one, wherever a chunk ends", () => {
    /* A last line of only `-`, `1.`, `#`, `+` or `***` interrupts a paragraph until the next
       character turns it into text ("1.5", "#hashtag", "***bold***"). */
    const text = "Total\n---\n\nNext **bold** para\n1.5 million items\n#hashtag here\n***bold*** start\n+1 vote\n\n- one\n- two\n\n```ts\nconst a = 1;\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n> quote\n\nTail";
    let previous = [];
    for (let end = 1; end <= text.length; end += 1) {
      const blocks = markdownBlocks(text.slice(0, end), []);
      assert.deepEqual(blocks.slice(0, previous.length - 1), previous.slice(0, -1), JSON.stringify(text.slice(0, end)));
      const live = renderBlock(blocks.at(-1), [], true);
      assert.ok(live.kind === "code" || live.html, `empty live block at ${JSON.stringify(text.slice(0, end))}`);
      previous = blocks;
    }
    assert.deepEqual(markdownBlocks("Total\n--", []), ["Total\n--"]);
    assert.deepEqual(markdownBlocks("Total\n---\n\nNext", []), ["Total", "---", "Next"]);
  });

  it("renders each block from the template in a keyed each, its own derived value per block", () => {
    const source = readFileSync(new URL("./MarkdownMessage.svelte", import.meta.url), "utf8");
    assert.match(source, /\{#each blocks as source, index \(index\)\}\s*\{@const live = index === liveIndex\}\s*\{@const block = renderBlock\(source, links, live\)\}/);
  });

  it("still attaches citations", () => {
    const text = "Hours are 9 to 5.";
    const citations = [{ type: "file_citation", file_name: "hours.md", start_index: 0, end_index: 16 }];
    const body = markup({ text, citations });
    assert.match(body, /class="md-citation"[^>]*data-cite-title="hours\.md"[^>]*>\[1\]<\/a>/);
    const [block] = markdownBlocks(text, citations);
    assert.match(renderBlock(block, citationLinks(citations), false).html, /md-citation/);
  });

  it("announces a citation's title, where it leads, and that it opens a new tab", () => {
    const text = "Hours are 9 to 5.";
    const citations = [
      { type: "file_citation", file_name: "hours.md", start_index: 0, end_index: 16, custom_metadata: { deep_url: "https://example.com/hours", heading: "Opening hours" } }
    ];
    const body = markup({ text, citations });
    const [link] = citationLinks(citations);
    assert.equal(citationAriaLabel(link), "Opening hours, example.com, opens in a new tab");
    assert.match(body, /class="md-citation"[^>]*aria-label="Opening hours, example\.com, opens in a new tab"[^>]*data-cite-title="Opening hours"/);
  });

  it("lets each text block find its own direction and keeps code left to right", () => {
    const body = markup({ text: "שלום עולם\n\n```py\nx = 1\n```" });
    assert.match(body, /<div dir="auto"><p>שלום עולם<\/p><\/div>/);
    assert.match(body, /class="md-code-wrap[^"]*" dir="ltr"/);
  });
});

describe("code fences", () => {
  const only = (text) => {
    const blocks = markdownBlocks(text, []);
    assert.equal(blocks.length, 1, JSON.stringify(blocks));
    return renderBlock(blocks[0], [], false);
  };

  it("opens with tildes and closes only on a fence at least as long", () => {
    assert.deepEqual(only("~~~py\nx = 1\n~~~"), { kind: "code", code: "x = 1", language: "py", open: false });
    assert.equal(only("````md\n```js\ninner\n```\n````").code, "```js\ninner\n```");
  });

  it("draws a fence inside a list item in place, with the same chrome as a top-level one", () => {
    const { html } = only("1. Install:\n    ```bash\n    pip install <x>\n    ```\n2. Run");
    assert.match(html, /^<ol>\s*<li>[\s\S]*<div class="md-code-wrap" dir="ltr"><div class="md-code-head"><span class="code-language-tag">bash<\/span><\/div><pre class="md-code-block"><code>[\s\S]*&lt;x&gt;[\s\S]*<\/code><\/pre><\/div>[\s\S]*<li>Run<\/li>/);
    assert.match(only("- ```js\n  const a = 1\n  ```").html, /<li>\s*<div class="md-code-wrap"[\s\S]*md-syntax-keyword">const</);
  });

  it("copies exactly the code: inner blank lines kept, trailing ones dropped", () => {
    assert.equal(only("```ts\nconst a = 1;\n\nconst b = 2;\n\n\n```").code, "const a = 1;\n\nconst b = 2;");
    assert.match(markup({ text: "```ts\nconst a = 1;\n```" }), /<span class="copyable[\s\S]*?aria-label="Copy code"/);
  });

  it("draws an open fence plain while it streams, and highlights it once it closes", () => {
    const open = markup({ text: "Look:\n\n```ts\nconst a = 1", streaming: true });
    assert.match(open, /class="md-code-wrap[^"]*\bopen\b/);
    assert.match(open, /<code>const a = 1<\/code>/);
    assert.doesNotMatch(open, /md-syntax|Copy code/);

    const closed = markup({ text: "Look:\n\n```ts\nconst a = 1\n```", streaming: true });
    assert.doesNotMatch(closed, /class="md-code-wrap[^"]*\bopen\b/);
    assert.match(closed, /md-syntax-keyword">const</);
    assert.match(closed, /aria-label="Copy code"/);
  });

  it("treats a closed fence followed by a whitespace-only line as closed", () => {
    const body = markup({ text: "```js\nconst a = 1\n```\n   ", streaming: true });
    assert.doesNotMatch(body, /class="md-code-wrap[^"]*\bopen\b/);
    assert.match(body, /aria-label="Copy code"/);
  });

  it("highlights an unclosed fence once the reply has ended", () => {
    assert.match(markup({ text: "```ts\nconst a = 1" }), /md-syntax-keyword">const</);
  });
});

describe("unfinished markup in the live block", () => {
  it("is closed only in the last block, and only while streaming", () => {
    const text = "First **unclosed\n\nSecond **bo";
    const live = markup({ text, streaming: true });
    assert.match(live, /<p>First \*\*unclosed<\/p>/);
    assert.match(live, /<p>Second <strong>bo<\/strong><\/p>/);
    assert.match(markup({ text }), /<p>Second \*\*bo<\/p>/);
  });

  it("keeps text it would otherwise cut as an unfinished tag", () => {
    assert.match(markup({ text: "if a <b then", streaming: true }), /if a &lt;b then/);
  });

  it("shows a half-typed link as its text, not remend's placeholder URL", () => {
    const body = markup({ text: "See [the docs](https://exa", streaming: true });
    assert.match(body, /See the docs/);
    assert.doesNotMatch(body, /<a |streamdown:/);
  });
});

describe("links", () => {
  const anchor = (href, label) => `<a class="md-link" href="${href}" target="_blank" rel="noreferrer">${label}</a>`;
  const hrefs = (body) => [...body.matchAll(/href="([^"]*)"/g)].map((match) => match[1].replaceAll("&amp;", "&"));

  it("draws a link we won't follow as its text", () => {
    for (const href of ["javascript:alert(1)", "<java\tscript:alert(1)>", "data:text/html,x", "vbscript:x", "tel:+15550100"]) {
      const body = markup({ text: `[click me](${href}).` });
      assert.match(body, /<p>click me\.<\/p>/);
      assert.doesNotMatch(body, /<a |target=|href=/);
    }
  });

  it("never sends a relative-looking link to another origin", () => {
    const page = new URL("http://127.0.0.1:5173/chat");
    for (const href of ["//evil.example/x", "/\\evil.example/x", "</\t/evil.example/x>", "\\/evil.example/x", "/.//evil.example/x"]) {
      for (const url of hrefs(markup({ text: `[p](${href})` }))) assert.equal(new URL(url, page).origin, page.origin, `${href} -> ${url}`);
    }
    assert.ok(markup({ text: "[p](/docs/a#b)" }).includes(anchor("/docs/a#b", "p")));
  });

  it("emits the normalized URL, never the raw one", () => {
    assert.ok(markup({ text: "[docs](https://example.com)" }).includes(anchor("https://example.com/", "docs")));
    assert.deepEqual(hrefs(markup({ text: "[x](https://exam\u200Bple.com/a)" })), [new URL("https://exam\u200Bple.com/a").href]);
    assert.deepEqual(hrefs(markup({ text: "see HTTPS://Exam\u200Bple.com/a" })), [new URL("https://exam\u200Bple.com/a").href]);
    const citations = [{ type: "file_citation", file_name: "a.md", custom_metadata: { deep_url: "//evil.example/a" } }];
    assert.match(markup({ text: "Fact.", citations }), /class="md-citation" href="#"[^>]*data-cite-url="#"/);
  });

  it("draws an image as its alt text and never loads it, finished or half-typed", () => {
    for (const [text, streaming] of [["![a chart](https://evil.example/p.png)", false], ["![a chart](https://evil.ex", true]]) {
      const body = markup({ text, streaming });
      assert.match(body, /<p>a chart<\/p>/);
      assert.doesNotMatch(body, /<img|evil|streamdown:/);
    }
  });

  it("draws an allowed image as a SafeImage placeholder holding its alt text", () => {
    const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
    const blob = `/api/blobs/${"c".repeat(64)}.bin`;
    for (const src of [png, blob]) {
      const body = markup({ text: `Here: ![a "red" <square>](${src})` });
      assert.ok(body.includes(`<span class="md-image" data-safe-image="" data-src="${src}" data-alt="a &quot;red&quot; &lt;square&gt;">a &quot;red&quot; &lt;square&gt;</span>`), body);
      assert.doesNotMatch(body, /<img/);
    }
  });

  it("draws a remote, SVG or near-miss blob image as its alt text, with no placeholder", () => {
    for (const src of ["https://evil.example/p.png", "data:image/svg+xml;base64,PHN2Zz4=", `/api/blobs/../${"c".repeat(64)}.bin`]) {
      const body = markup({ text: `![a chart](${src})` });
      assert.match(body, /<p>a chart<\/p>/);
      assert.doesNotMatch(body, /data-safe-image|<img|evil|svg/);
    }
  });

  it("mounts each placeholder from an effect that is told when any block changes", () => {
    const source = readFileSync(new URL("./MarkdownMessage.svelte", import.meta.url), "utf8");
    assert.match(source, /void \[blocks, liveIndex, links\];/);
    assert.match(source, /querySelectorAll<HTMLElement>\("\[data-safe-image\]"\)/);
    assert.match(source, /if \(image\.host\.parentNode !== slot\) slot\.replaceChildren\(image\.host\);/);
  });

  it("keeps a parenthesised part of a URL inside the link", () => {
    assert.match(markup({ text: "[Foo](https://en.wikipedia.org/wiki/Foo_(bar))" }), /href="https:\/\/en\.wikipedia\.org\/wiki\/Foo_\(bar\)"[^>]*>Foo<\/a><\/p>/);
  });

  it("links a bare or angled URL, without trailing punctuation", () => {
    const cases = [
      ["see HTTPS://Example.com/a now", "https://example.com/a", "HTTPS://Example.com/a"],
      ["x <https://example.com> y", "https://example.com/", "https://example.com"],
      ["https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)"],
      ["(see https://e.example/a)", "https://e.example/a", "https://e.example/a"],
      ["Done: https://e.example/a.", "https://e.example/a", "https://e.example/a"],
      ["Is it https://e.example/a?", "https://e.example/a", "https://e.example/a"],
      ["https://e.example/a, then", "https://e.example/a", "https://e.example/a"],
      ["write to mailto:a@b.example", "mailto:a@b.example", "mailto:a@b.example"]
    ];
    for (const [text, href, label] of cases) assert.ok(markup({ text }).includes(anchor(href, label)), text);
  });

  it("leaves a citation after a bare URL, and a URL inside a word or link label, alone", () => {
    const body = markup({ text: "Read https://e.example[1]", citations: [{ type: "file_citation", file_name: "e.md" }] });
    assert.ok(body.includes(anchor("https://e.example/", "https://e.example")));
    assert.match(body, /class="md-citation"[^>]*>\[1\]<\/a>/);
    assert.doesNotMatch(markup({ text: "xhttps://e.example" }), /<a /);
    assert.deepEqual(hrefs(markup({ text: "[https://e.example](https://other.example)" })), ["https://other.example/"]);
  });
});

describe("citations", () => {
  const two = [{ type: "file_citation", file_name: "one.md" }, { type: "file_citation", file_name: "two.md" }];
  const marker = (n) => `&nbsp;<a class="md-citation"[^>]*>\\[${n}\\]</a>`;

  it("moves a leading citation run to the end of its own line", () => {
    assert.match(markup({ text: "[1] first line\n[2] second line", citations: two }), new RegExp(`<p>\\s*first line${marker(1)}\n\\s*second line${marker(2)}</p>`));
  });

  it("places a leading citation after a list item, cell or heading, and glues one after a link", () => {
    assert.match(markup({ text: "- [1] item", citations: two }), new RegExp(`<li>\\s*item${marker(1)}</li>`));
    assert.match(markup({ text: "# [2] Head", citations: two }), new RegExp(`<h3>\\s*Head${marker(2)}</h3>`));
    assert.match(markup({ text: "| a | b |\n|---|---|\n| [1] x | y |", citations: two }), new RegExp(`<td>\\s*x${marker(1)}</td>`));
    assert.match(markup({ text: "See [docs](https://d.example)[2]", citations: two }), new RegExp(`docs</a>${marker(2)}`));
  });
});

describe("markdown as plain text, for previews and tooltips", () => {
  it("drops emphasis, code and link markup, keeping the words", () => {
    assert.equal(markdownToText("**Berlin** and _Lisbon_, ~~Rome~~"), "Berlin and Lisbon, Rome");
    assert.equal(markdownToText("run `pip install` then [read the docs](https://d.example)"), "run pip install then read the docs");
    assert.equal(markdownToText("![a chart](https://e.example/x.png) of fares"), "a chart of fares");
    assert.equal(markdownToText(`![a chart](/api/blobs/${"c".repeat(64)}.png) of fares`), "a chart of fares");
  });

  it("reads headings, lists, quotes, tables and fences as lines of text", () => {
    assert.equal(markdownToText("# Plan\n\n- **one**\n- two\n\n1. three"), "Plan\none\ntwo\nthree");
    assert.equal(markdownToText("> quoted"), "quoted");
    assert.equal(markdownToText("| a | b |\n|---|---|\n| 1 | 2 |"), "a b\n1 2");
    assert.equal(markdownToText("```py\nprint(1)\n```"), "print(1)");
  });

  it("keeps HTML-looking text as text, never markup", () => {
    assert.equal(markdownToText("<script>alert(1)</script> **bold**"), "<script>alert(1)</script> bold");
    assert.equal(markdownToText("<b>not bold</b> &amp;"), "<b>not bold</b> &amp;");
  });

  it("returns text with nothing to parse as it is, and empty for nothing", () => {
    const plain = "When should I use a local activity? (2 options, 50% faster)";
    assert.equal(markdownToText(plain), plain);
    assert.equal(markdownToText("read_file and x_y_z stay whole"), "read_file and x_y_z stay whole");
    assert.equal(markdownToText(null), "");
    assert.equal(markdownToText(undefined), "");
    assert.equal(markdownToText(""), "");
  });
});

describe("the shared renderer outside the chat", () => {
  const html = (component, props) => stripComments(render(component, { props }).body);

  it("draws an ask_user question as markdown", () => {
    const body = html(CallInput, { input: { question: "Book **TAP 1234**?\n\n- window seat" } });
    assert.match(body, /class="markdown-message/);
    assert.match(body, /<strong>TAP 1234<\/strong>/);
    assert.match(body, /<li>window seat<\/li>/);
  });

  it("draws a gated script as the shared highlighted, copyable code block", () => {
    const body = html(CallInput, { input: { script: "import asyncio\nprint('hi')" } });
    assert.match(body, /<dd class="block[^"]*"><div class="markdown-message compact[\s\S]*?class="md-code-wrap/);
    assert.match(body, /aria-label="Copy code"/);
    assert.match(body, /<span class="code-language-tag">python<\/span>/);
    assert.match(body, /md-syntax-keyword[^>]*>import</);
  });

  it("draws a field's description as compact markdown", () => {
    const form = resultForm({ type: "object", properties: { seat: { type: "string", description: "Prefer `window`, **not** aisle." } } });
    const body = html(SchemaForm, { fields: form.fields, values: emptyValues(form.fields), idPrefix: "t" });
    assert.match(body, /class="markdown-message compact/);
    assert.match(body, /<code>window<\/code>/);
  });

  it("draws an agent's description as markdown in its details and as plain text in its row", () => {
    const agent = { key: "qa", workflow_type: "QaAgent", task_queue: "q", label: "Q&A", description: "Uses a *durable* **workflow**.", worker: { status: "ready" } };
    const body = html(StartSessionDialog, { agents: [agent], onStart() {}, onClose() {} });
    assert.match(body, /<span class="agent-description[^"]*"[^>]*>Uses a durable workflow\.<\/span>/);
    assert.match(body, /class="detail-description[^"]*"[^>]*><div class="markdown-message compact[\s\S]*<em>durable<\/em>/);
  });

  it("marks a paragraph that is one bold line as a lead, so the chat can space it as a heading", () => {
    const body = markup({ text: '**1. An Interactive "Trip Board"**\n\n**Real IDs:** every flight is real.\n\n__Next__' });
    assert.equal(body.match(/ data-lead="true"/g)?.length, 2);
    assert.match(body, /<div dir="auto" data-lead="true"><p><strong>1\. An Interactive/);
    assert.match(body, /<div dir="auto"><p><strong>Real IDs:<\/strong> every/);
  });

  it("gives compact density its own class, so a card need not restyle the chat's", () => {
    assert.match(markup({ text: "x", compact: true }), /class="markdown-message compact/);
    assert.doesNotMatch(markup({ text: "x" }), /compact/);
    /* Same specificity as the base rules, so it only wins by coming after them. */
    const source = readFileSync(new URL("./MarkdownMessage.svelte", import.meta.url), "utf8");
    assert.ok(source.lastIndexOf(".markdown-message :global(") < source.indexOf(".compact :global(code)"), "compact rules must follow every base rule");
  });
});
