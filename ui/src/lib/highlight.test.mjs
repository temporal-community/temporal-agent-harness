// ABOUTME: Pins the one highlighter every code surface draws through: it escapes every token it
// emits (so `<script>` in a string, comment, regex or identifier stays text), maps the aliases a
// fence or file name uses, reads a regex literal as a regex (a quote inside one once turned the rest
// of the file into a string), and gives up to plain text past the size cap.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { HIGHLIGHT_LIMIT, highlight } from "./highlight.ts";

/** Every `<` in the output opens or closes one of our spans, so nothing else became markup. */
function onlyOurSpans(html) {
  const opened = html.split('<span class="md-syntax-').length - 1;
  assert.equal(html.split("<").length - 1, opened * 2, html);
  assert.doesNotMatch(html, /<script|<img/i);
}

const HOSTILE = `<script>alert("x")</script> & <img src=x onerror='y'>`;

describe("highlight", () => {
  const samples = {
    ts: `// ${HOSTILE}\nconst s = "${HOSTILE}"; const t = \`${HOSTILE}\`; s.replace(/<script>/g, "");\nconst el = <div title="${HOSTILE}">x</div>;`,
    tsx: `const el = <a href="${HOSTILE}">{"${HOSTILE}"}</a>;`,
    json: `{"${HOSTILE}": "${HOSTILE}", "n": 1, "b": true}`,
    python: `# ${HOSTILE}\ns = """${HOSTILE}"""\ndef f(): return '${HOSTILE}'`,
    shell: `echo "${HOSTILE}" # ${HOSTILE}\nexport A='${HOSTILE}'`,
    yaml: `key: "${HOSTILE}" # ${HOSTILE}\nlist: [1, true]`,
    toml: `key = "${HOSTILE}" # ${HOSTILE}`,
    diff: `--- a\n+++ b\n- ${HOSTILE}\n+ ${HOSTILE}`,
    markdown: `# ${HOSTILE}\n\`${HOSTILE}\` [x](${HOSTILE})`,
    dockerfile: `FROM node # ${HOSTILE}\nRUN echo "${HOSTILE}"`,
    env: `A="${HOSTILE}" # ${HOSTILE}`,
    go: `// ${HOSTILE}\nfunc main() { s := "${HOSTILE}" }`,
    sql: `-- ${HOSTILE}\nSELECT '${HOSTILE}' FROM t`,
    cpp: `// ${HOSTILE}\n#include <stdio.h>\nint main() { const char *s = "${HOSTILE}"; char c = '<'; }`,
    java: `// ${HOSTILE}\nclass A { String s = "${HOSTILE}"; char c = '<'; }`,
    rust: `// ${HOSTILE}\nfn main() { let s = "${HOSTILE}"; let r = r#"${HOSTILE}"#; }`,
    ruby: `# ${HOSTILE}\ndef f; s = "${HOSTILE}"; t = '${HOSTILE}'; end`,
    css: `/* ${HOSTILE} */\na[title="${HOSTILE}"]::after { content: '${HOSTILE}'; color: red; }`,
    html: `<!-- ${HOSTILE} -->\n<div title="${HOSTILE}">${HOSTILE}</div>\n<script>const s = "${HOSTILE}";</script>`,
    svelte: `<script>let s = "${HOSTILE}";</script>\n<p title="${HOSTILE}">{s} ${HOSTILE}</p>\n<style>p { content: '${HOSTILE}'; }</style>`
  };

  for (const [language, code] of Object.entries(samples)) {
    it(`escapes every token in ${language}`, () => {
      const html = highlight(code, language);
      assert.match(html, /md-syntax-/, "it highlighted something");
      onlyOurSpans(html);
    });
  }

  it("maps the aliases fences and file names use", () => {
    const aliases = { typescript: "ts", javascript: "ts", js: "ts", jsx: "tsx", py: "python", bash: "shell", sh: "shell", zsh: "shell", yml: "yaml", md: "markdown", patch: "diff", golang: "go", dotenv: "env", docker: "dockerfile", c: "cpp", h: "cpp", hpp: "cpp", rs: "rust", rb: "ruby", htm: "html" };
    for (const [alias, language] of Object.entries(aliases)) {
      assert.equal(highlight(samples[language], alias), highlight(samples[language], language), alias);
    }
  });

  it("draws an unknown language escaped and plain", () => {
    assert.equal(highlight("a <b> & 'c'", "cobol"), "a &lt;b&gt; &amp; &#39;c&#39;");
  });

  it("reads a regex literal with a quote in it as a regex", () => {
    const html = highlight(`s.replace(/'/g, "");\nconst n = 1;`, "ts");
    assert.match(html, /\n<span class="md-syntax-keyword">const<\/span>/);
  });

  it("is plain text past the size cap", () => {
    assert.doesNotMatch(highlight(`"${"y".repeat(HIGHLIGHT_LIMIT.chars)}"`, "json"), /<span/);
    assert.doesNotMatch(highlight("1\n".repeat(HIGHLIGHT_LIMIT.lines), "json"), /<span/);
    assert.match(highlight("1\n".repeat(HIGHLIGHT_LIMIT.lines - 1), "json"), /<span/);
  });
});
