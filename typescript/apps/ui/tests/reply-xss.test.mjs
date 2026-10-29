// ABOUTME: Runs hostile model and tool output through the console's real render paths — a chat
// reply's markdown (finished, streaming, and every streamed prefix), its citations, code fences and
// tables, a structured JSON reply, and the Logs pane's citation links — and asserts nothing in the
// markup can run: no script-capable element, no on* attribute, no javascript:, vbscript: or
// data: URL, and no attribute a payload has broken out of. The scan is strict, so an unescaped `<`
// or an unquoted attribute anywhere fails it rather than slipping past a looser pattern.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parse } from "svelte/compiler";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { highlight } from "../src/lib/highlight.ts";
import JsonReply from "../src/lib/components/chat/JsonReply.svelte";
import MarkdownMessage, { citationLinks } from "../src/lib/components/chat/MarkdownMessage.svelte";
import { stripComments } from "./support/controllerHarness.mjs";

const FORBIDDEN_TAGS = new Set([
  "script", "iframe", "frame", "frameset", "object", "embed", "applet", "img", "image", "video", "audio",
  "source", "picture", "style", "link", "meta", "base", "form", "input", "textarea", "math", "template",
  "noscript", "animate", "set", "foreignobject", "portal"
]);
const URL_ATTRIBUTES = new Set(["href", "src", "srcset", "action", "formaction", "xlink:href", "poster", "data", "background", "ping"]);
const TAG = /<\/?([a-z][a-z0-9-]*)((?:\s+[a-z][a-z0-9:_-]*(?:="[^"]*")?)*)\s*\/?>/iy;
const ATTRIBUTE = /\s+([a-z][a-z0-9:_-]*)(?:="([^"]*)")?/gi;

const decode = (value) =>
  value
    .replace(/&#x([0-9a-f]+);?/gi, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);?/g, (_, dec) => String.fromCodePoint(Number(dec)))
    .replace(/&(quot|apos|lt|gt|colon|tab|newline|amp);/g, (_, name) => ({ quot: '"', apos: "'", lt: "<", gt: ">", colon: ":", tab: "\t", newline: "\n", amp: "&" })[name]);

/** A URL as a browser reads it: entities decoded, then whitespace and control characters dropped. */
function assertSafeUrl(raw, where) {
  const url = decode(raw).replace(/[\u0000-\u0020\u007f-\u009f]/g, "").toLowerCase();
  const scheme = /^([a-z][a-z0-9+.-]*):/.exec(url)?.[1];
  assert.ok(scheme == null || ["http", "https", "mailto"].includes(scheme), `${where}: unsafe URL ${JSON.stringify(raw)}`);
  assert.ok(!/^[/\\]{2}/.test(url), `${where}: off-origin URL ${JSON.stringify(raw)}`);
}

/** Every tag parses strictly, and none of them, nor any attribute, can run code. */
function assertInert(html, where) {
  let tags = 0;
  for (let at = html.indexOf("<"); at !== -1; at = html.indexOf("<", at)) {
    TAG.lastIndex = at;
    const match = TAG.exec(html);
    assert.ok(match, `${where}: raw "<" or malformed tag at ${JSON.stringify(html.slice(at, at + 80))}`);
    const name = match[1].toLowerCase();
    assert.ok(!FORBIDDEN_TAGS.has(name), `${where}: <${name}> in ${JSON.stringify(match[0])}`);
    for (const [, attribute, value = ""] of match[2].matchAll(ATTRIBUTE)) {
      const key = attribute.toLowerCase();
      assert.ok(!key.startsWith("on"), `${where}: ${key} on <${name}>`);
      assert.ok(key !== "srcdoc", `${where}: srcdoc on <${name}>`);
      if (URL_ATTRIBUTES.has(key)) assertSafeUrl(value, `${where} <${name} ${key}>`);
      if (key === "style") assert.doesNotMatch(decode(value), /url\(|expression\(|javascript:|@import/i, `${where}: style ${value}`);
      if (key.startsWith("data-") && key.endsWith("-url")) assertSafeUrl(value, `${where} <${name} ${key}>`);
    }
    tags += 1;
    at = TAG.lastIndex;
  }
  assert.ok(tags > 0, `${where}: nothing rendered`);
}

const markup = (props) => stripComments(render(MarkdownMessage, { props }).body);

describe("the scan", () => {
  it("catches each thing it looks for", () => {
    const bad = [
      "<p><script>x</script></p>", "<p>a < b</p>", '<a href="java&#x61;script:x">', '<a href="\tjavascript:x">', '<a href="//evil.example">',
      '<a href=javascript:x>', '<span onclick="x">', '<span x=""onclick="y">', '<svg><animate attributename="href"></svg>',
      '<div style="background:url(x)">', '<a data-cite-url="vbscript:x">', '<iframe srcdoc="x">'
    ];
    for (const html of bad) assert.throws(() => assertInert(html, "self-check"), undefined, html);
    assertInert('<a class="md-link" href="https://ok.example/" target="_blank" rel="noreferrer">x &lt;y&gt;</a>', "self-check");
  });
});

/** The finished reply, the same text still streaming, and every prefix of it as it arrives. */
function assertInertWhileStreaming(text, props = {}) {
  assertInert(markup({ ...props, text }), `finished ${JSON.stringify(text)}`);
  for (let end = 1; end <= text.length; end += 1) {
    assertInert(markup({ ...props, text: text.slice(0, end), streaming: true }), `streaming ${JSON.stringify(text.slice(0, end))}`);
  }
}

const SCHEMES = [
  "javascript:alert(1)",
  "JaVaScRiPt:alert(1)",
  "vbscript:msgbox(1)",
  "VBScript:msgbox(1)",
  "data:text/html,<script>alert(1)</script>",
  "DATA:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
  "jav&#x61;script:alert(1)",
  "jav&#97;script:alert(1)",
  "javascript&colon;alert(1)",
  " javascript:alert(1)",
  "\tjavascript:alert(1)",
  "java\tscript:alert(1)",
  "\u0001javascript:alert(1)",
  "java\u0000script:alert(1)",
  "%6A%61%76%61%73%63%72%69%70%74:alert(1)"
];

describe("a reply's markdown", () => {
  const payloads = {
    "script tag": "<script>alert(1)</script>",
    "img onerror": '<img src=x onerror="alert(1)">',
    "svg onload": "<svg onload=alert(1)><circle r=5 /></svg>",
    iframe: '<iframe src="javascript:alert(1)"></iframe>',
    "object and embed": '<object data="javascript:alert(1)"></object><embed src="data:text/html,x">',
    "style and link": '<style>*{background:url("javascript:alert(1)")}</style><link rel=stylesheet href="javascript:x">',
    "html in emphasis": "**<b onmouseover=alert(1)>bold</b>** and _<i>x</i>_",
    "html in a heading": "# <script>alert(1)</script>",
    "html in a quote": "> <img src=x onerror=alert(1)>",
    "html in inline code": "`<script>alert(1)</script>`",
    "comment and cdata": "<!-- <script>alert(1)</script> --><![CDATA[<script>]]>",
    "attribute breakout in a link title": '[x](https://ok.example "a\\" onmouseover=\\"alert(1)") and [y](https://ok.example \'b\\\' onfocus=alert(1)\')',
    "quote in a link destination": '[x](https://ok.example/"onmouseover="alert(1))',
    "angled destination with quote": '[x](<https://ok.example/" onmouseover="alert(1)>)',
    "reference-style link": "[x][r]\n\n[r]: javascript:alert(1)",
    "reference-style link with title": '[x][r]\n\n[r]: https://ok.example "t\\" onmouseover=\\"alert(1)"',
    "footnote definition": "a[^1]\n\n[^1]: <script>alert(1)</script>",
    "autolink javascript": "<javascript:alert(1)>",
    "autolink vbscript": "<vbscript:msgbox(1)>",
    "autolink data": "<data:text/html,<script>alert(1)</script>>",
    "bare javascript": "see javascript:alert(1) now",
    "bare URL with quote": 'see https://ok.example/"onmouseover="alert(1) now',
    "angled URL with quote": '<https://ok.example/"onmouseover="alert(1)>',
    "mailto with quote": '<mailto:a@b.example"onmouseover="alert(1)>',
    "link inside a link label": "[[x](javascript:alert(1))](https://ok.example)",
    "image javascript": "![x](javascript:alert(1))",
    "image data html": "![x](data:text/html,<script>alert(1)</script>)",
    "image with onerror alt": '![" onerror="alert(1)](https://evil.example/p.png)',
    "linked image": "[![x](https://evil.example/p.png)](javascript:alert(1))",
    "allowed image with hostile alt": '![" onerror="alert(1)` <script>](data:image/png;base64,iVBORw0KGgo=) ![x" onload="y](/api/blobs/' + "a".repeat(64) + ".bin)",
    "rtl override in link text": "[\u202Egnp.exe](https://ok.example/a) and [\u2067x\u2069](https://ok.example/b)",
    "zero-width and bidi in destination": "[x](\u200Bjavascript:alert(1)) [y](\u202Ejavascript:alert(1))",
    "entities in text": "&lt;script&gt;alert(1)&lt;/script&gt; &#60;img src=x onerror=alert(1)&#62;",
    "unclosed link": "[click](javascript:alert(1)",
    "unclosed link label": "[click <img src=x onerror=alert(1)>",
    "unclosed fence with html": "```html\n<script>alert(1)</script>\n<img src=x onerror=alert(1)>",
    "unclosed emphasis around html": "**<script>alert(1)</script>",
    "unclosed inline code": "`<img src=x onerror=alert(1)>"
  };
  for (const [name, text] of Object.entries(payloads)) {
    it(`renders ${name} inert, finished and at every point while streaming`, () => assertInertWhileStreaming(text));
  }

  it("follows no link, autolink or image with an executable or encoded scheme", () => {
    for (const url of SCHEMES) {
      for (const text of [`[x](${url})`, `[x](<${url}>)`, `<${url}>`, `![x](${url})`, `[x][r]\n\n[r]: ${url}`, `see ${url}`]) {
        const body = markup({ text });
        assertInert(body, text);
        assert.doesNotMatch(body, /<a [^>]*class="md-link"/, `${text} became a link`);
      }
    }
  });

  it("draws raw HTML as text rather than dropping it", () => {
    assert.match(markup({ text: "<script>alert(1)</script>" }), /&lt;script>alert\(1\)&lt;\/script>|&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
    assert.match(markup({ text: '<img src=x onerror="alert(1)">' }), /&lt;img src=x onerror=(&quot;|")alert\(1\)(&quot;|")/);
  });

  it("keeps a bidi override inside the link's text, not its URL", () => {
    const body = markup({ text: "[\u202Egnp.exe](https://ok.example/a)" });
    assert.ok(body.includes('href="https://ok.example/a"'));
    assert.ok(body.includes(">\u202Egnp.exe</a>"));
  });
});

describe("a reply's code fences", () => {
  const languages = ['js" onmouseover="alert(1)', "js' onmouseover='alert(1)", "<script>alert(1)</script>", 'x"><img src=x onerror=alert(1)>', "`onmouseover=alert(1)`"];

  it("draws a hostile fence language as text, at the top level, in a list and in a quote", () => {
    for (const lang of languages) {
      for (const text of [`\`\`\`${lang}\ncode\n\`\`\``, `- item\n\n  \`\`\`${lang}\n  code\n  \`\`\``, `> \`\`\`${lang}\n> code\n> \`\`\``, `~~~${lang}\ncode\n~~~`]) {
        assertInertWhileStreaming(text);
      }
    }
  });

  it("draws a hostile language prop, and hostile code in every highlighted language, as text", () => {
    const code = '</span></code></pre><script>alert(1)</script><img src=x onerror="alert(1)"> "\' `${x}` <!-- -->';
    for (const language of [...languages, "html", "svelte", "js", "json", "markdown", "css", "py", "sh", "diff"]) {
      assertInert(markup({ text: code, language }), `language ${language}`);
      assertInert(`<code>${highlight(code, language)}</code>`, `highlight ${language}`);
    }
    for (const lang of ["html", "svelte", "markdown", "js"]) assertInertWhileStreaming(`\`\`\`${lang}\n${code}\n\`\`\``);
  });
});

describe("a reply's tables", () => {
  const tables = {
    "table tags in cells": "| <table> | </table><script>alert(1)</script> |\n|---|---|\n| <table onmouseover=alert(1)> | </table> |",
    "code spans of table tags": "| `<table>` | `</table>` |\n|---|---|\n| `<div class=\"md-table-wrap\">` | x |",
    "links and images in cells": "| [x](javascript:alert(1)) | ![y](data:text/html,x) |\n|---|---|\n| <https://ok.example/\"onmouseover=\"x> | [z](vbscript:x) |",
    "html in a header cell": "| <img src=x onerror=alert(1)> |\n|---|\n| a |",
    "unclosed table row": "| a | b |\n|---|---|\n| <script>alert(1)"
  };
  for (const [name, text] of Object.entries(tables)) {
    it(`renders ${name} inert, with exactly one wrapper per table`, () => {
      assertInertWhileStreaming(text);
      const body = markup({ text });
      assert.equal(body.split('<div class="md-table-wrap">').length, body.split("<table>").length);
    });
  }
});

describe("a reply's citations", () => {
  const hostile = [
    { heading: '"><script>alert(1)</script>', deep_url: "javascript:alert(1)" },
    { heading: "' onmouseover='alert(1)", deep_url: "JaVaScRiPt:alert(1)" },
    { title: "`onfocus=alert(1)`", deep_url: "vbscript:msgbox(1)" },
    { heading: "<img src=x onerror=alert(1)>", deep_url: "data:text/html,<script>alert(1)</script>" },
    { heading: "x", deep_url: 'https://ok.example/" onmouseover="alert(1)' },
    { heading: "y", deep_url: "jav&#x61;script:alert(1)" },
    { heading: "z", deep_url: " \tjavascript:alert(1)" },
    { heading: "\u202Etxt.exe", deep_url: "//evil.example/a" }
  ];
  const citations = hostile.map((custom_metadata, n) => ({ type: "file_citation", file_name: `f${n}.md`, custom_metadata }));
  const placed = hostile.map((custom_metadata, n) => ({ type: "file_citation", document_uri: "javascript:alert(2)", start_index: 0, end_index: 4 + n, custom_metadata }));

  it("draws hostile citation titles and URLs inert, appended, placed, and explicit", () => {
    assertInertWhileStreaming("Fact one. Fact two.", { citations });
    assertInertWhileStreaming("Fact one and fact two and fact three.", { citations: placed });
    assertInertWhileStreaming("Fact [1] and [2], [3] [4] [5] [6] [7] [8].", { citations });
    assertInertWhileStreaming("- item [1]\n\n| a [2] |\n|---|\n| b [3] |\n\n# h [4]", { citations });
  });

  it("gives every citation link a followable URL or none", () => {
    for (const link of citationLinks(citations)) assertSafeUrl(link.url, `citation ${link.title}`);
  });
});

describe("a structured JSON reply", () => {
  const json = {
    "<script>alert(1)</script>": '"><img src=x onerror=alert(1)>',
    "</span><svg onload=alert(1)>": ["javascript:alert(1)", "<iframe src=javascript:alert(1)>", "\u202E", "</code></pre><script>"],
    nested: { "' onmouseover='x": "`${alert(1)}`", html: "<!-- --><style>*{}</style>" }
  };

  it("draws HTML in keys and string values as text, finished, folded and streaming", () => {
    assertInert(stripComments(render(JsonReply, { props: { json } }).body), "finished");
    const big = { rows: Array.from({ length: 30 }, () => json) };
    assertInert(stripComments(render(JsonReply, { props: { json: big } }).body), "folded");
    const text = JSON.stringify(json, null, 2);
    for (const streamed of [text, `\`\`\`json\n${text}`, text.slice(0, 40), "<script>alert(1)</script>"]) {
      assertInert(stripComments(render(JsonReply, { props: { text: streamed } }).body), `streaming ${streamed.slice(0, 30)}`);
    }
  });
});

describe("the graph details dialog", () => {
  const source = stripComments(readFileSync(new URL("../src/lib/components/flow/AgentStateFlow.svelte", import.meta.url), "utf8"));

  it("draws raw HTML only from highlight(), which escapes hostile JSON and Python", () => {
    const uses = [...source.matchAll(/\{@html ([^}]*)\}/g)].map((match) => match[1]);
    assert.ok(uses.length > 0 && uses.every((expr) => expr.startsWith("highlight(")), `unexpected {@html}: ${uses.join(", ")}`);
    const code = '</code></pre><script>alert(1)</script><img src=x onerror="alert(1)">';
    for (const language of ["json", "python"]) {
      assertInert(`<code>${highlight(JSON.stringify({ [code]: code }, null, 2), language)}</code>`, `dialog ${language}`);
    }
  });
});

describe("the Logs pane's citation links", () => {
  const source = readFileSync(new URL("../src/lib/components/agent/TranscriptPanel.svelte", import.meta.url), "utf8");

  it("takes every href from citationLinks, which allows only a followable URL", () => {
    const anchors = [];
    const walk = (node) => {
      if (node == null || typeof node !== "object") return;
      if (node.type === "RegularElement" && node.name === "a") anchors.push(node);
      for (const value of Object.values(node)) if (value && typeof value === "object") walk(value);
    };
    walk(parse(source, { modern: true }).fragment);
    assert.ok(anchors.length > 0, "the pane still draws citation anchors");
    for (const anchor of anchors) {
      const href = anchor.attributes.find((attribute) => attribute.name === "href");
      const expression = source.slice(href.value.expression.start, href.value.expression.end);
      assert.equal(expression, "link.url", `href={${expression}}`);
    }
    assert.match(source, /\{#each citationLinks\(row\.citations\) as link/);
  });
});
