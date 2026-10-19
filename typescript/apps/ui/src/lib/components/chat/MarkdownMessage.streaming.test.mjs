// A synchronous renderer loop cannot be interrupted by Vitest's test timeout. Compile the real
// component and render in a bounded child so a broken streaming prefix fails instead of hanging
// the test runner or consuming the browser's heap.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { compile } from "svelte/compiler";
import { describe, it } from "vitest";

const require = createRequire(import.meta.url);
const componentPath = new URL("./MarkdownMessage.svelte", import.meta.url);
const compiled = compile(readFileSync(componentPath, "utf8"), {
  filename: componentPath.pathname,
  generate: "server"
}).js.code.replaceAll(
  "'svelte/internal/server'",
  JSON.stringify(pathToFileURL(require.resolve("svelte/internal/server")).href)
);
const componentUrl = `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`;
const serverUrl = pathToFileURL(require.resolve("svelte/server")).href;
const childScript = `
  import { readFileSync } from "node:fs";
  const { render } = await import(${JSON.stringify(serverUrl)});
  const { default: MarkdownMessage } = await import(${JSON.stringify(componentUrl)});
  const inputs = JSON.parse(readFileSync(0, "utf8"));
  process.stdout.write(JSON.stringify(inputs.map(text => render(MarkdownMessage, { props: { text } }).body)));
`;

function renderPrefixes(inputs) {
  const result = spawnSync(
    process.execPath,
    ["--max-old-space-size=64", "--input-type=module", "--eval", childScript],
    { input: JSON.stringify(inputs), encoding: "utf8", timeout: 10_000, maxBuffer: 4 * 1024 * 1024 }
  );
  assert.equal(
    result.status,
    0,
    `Markdown rendering must terminate for every prefix (signal ${result.signal}, ` +
      `error ${result.error?.message ?? "none"}): ${result.stderr.slice(0, 700)}`
  );
  const output = JSON.parse(result.stdout);
  assert.equal(output.length, inputs.length);
  return output;
}

describe("streaming Markdown lists", () => {
  for (const marker of ["-", "*", "1.", "1)"]) {
    it(`consumes an empty ${marker} item while its contents are still arriving`, () => {
      const tag = marker.startsWith("1") ? "ol" : "ul";
      const [empty, complete] = renderPrefixes([
        `Intro\n\n${marker} `,
        `Intro\n\n${marker} **12** = 2² × 3`
      ]);
      assert.match(empty, new RegExp(`<${tag}(?:\\s[^>]*)?><li>\\s*</li></${tag}>`));
      assert.match(complete, new RegExp(`<${tag}(?:\\s[^>]*)?><li>(?:<p>)?<strong>12</strong> = 2² × 3(?:</p>)?</li></${tag}>`));
    });
  }

  it("preserves unsupported plus markers as paragraph text", () => {
    const [empty, complete] = renderPrefixes(["Intro\n\n+ ", "Intro\n\n+ **12** = 2² × 3"]);
    assert.ok(empty.includes("<p>+</p>"), empty);
    assert.ok(complete.includes("<p>+ <strong>12</strong> = 2² × 3</p>"), complete);
  });

  it("keeps an incomplete nested item inside its completed numbered parent", () => {
    const [html] = renderPrefixes(["1. **Prime factorization**  \n   - 12 = 2² × 3\n   - "]);
    assert.match(html, /<ol(?:\s[^>]*)?><li>(?:<p>)?<strong>Prime factorization<\/strong>\s*(?:<\/p>)?<ul><li>(?:<p>)?12 = 2² × 3(?:<\/p>)?<\/li><li>\s*<\/li><\/ul><\/li><\/ol>/);
  });

  it("keeps excessively nested markers visible without exhausting the call stack", () => {
    const [html] = renderPrefixes(["- ".repeat(4000) + "Detail"]);
    assert.ok(html.includes("Detail"), html);
    assert.ok(html.includes("- - -"), "unparsed markers remain visible at the nesting limit");
  });

  it("renders every prefix of a streamed response with nested lists", () => {
    // Every character boundary covers chunks where markers, their spacing, and their contents
    // arrive separately. The final structure must still number all three top-level steps.
    const text = "Let’s find the LCM of 12, 45, and 2 step by step.\n\n" +
      "1. **Prime factorization**  \n   - 12 = 2² × 3  \n   - 45 = 3² × 5  \n   - 2 = 2\n\n" +
      "2. **Take each prime with the highest power appearing in any number**  \n" +
      "   - For prime 2: highest power is 2²  \n   - For prime 3: highest power is 3²  \n" +
      "   - For prime 5: highest power is 5¹  \n\n" +
      "3. **Multiply them together**  \n   LCM = 2² × 3² × 5  \n   = 4 × 9 × 5  \n" +
      "   = 36 × 5  \n   = 180\n\n**LCM(12, 45, 2) = 180**";
    const prefixes = Array.from({ length: text.length + 1 }, (_, index) => text.slice(0, index));
    const output = renderPrefixes(prefixes);
    const complete = output.at(-1);
    assert.equal((complete.match(/<ol(?:\s[^>]*)?>/g) ?? []).length, 1, complete);
    assert.equal((complete.match(/<ul>/g) ?? []).length, 2, complete);
    assert.match(complete, /<\/ul><\/li><li>(?:<p>)?<strong>Take each prime/);
    assert.match(complete, /<\/ul><\/li><li>(?:<p>)?<strong>Multiply them together/);
    assert.ok(complete.includes("<strong>LCM(12, 45, 2) = 180</strong>"), complete);
  });
});
