// ABOUTME: Asserts that tool identity is painted with --tool and never with --warning. The console
// used to alias the two, so a tool span and a warning were literally the same colour and neither
// could move without dragging the other: the --retry token carries a comment about exactly that
// dead end. Temporal's own timeline keeps category (CATEGORY_STROKE_COLORS) and outcome
// (STATUS_STROKE_COLORS) as two maps that overlay, because a tool that warns is both at once.
// The two tokens start out on the same amber, which is what makes this file necessary rather than
// decorative: nothing on screen will tell anyone the split has been undone, and a single
// `var(--warning)` typed into a .tool rule quietly re-fuses them.

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it } from "vitest";

const root = fileURLToPath(new URL("../src/", import.meta.url));

function sourceFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = `${dir}${entry.name}`;
    if (entry.isDirectory()) return sourceFiles(`${path}/`);
    return /\.(svelte|css|ts)$/.test(entry.name) ? [path] : [];
  });
}

/* Comments are prose about tokens, not tokens — app.css explains the split in one, and this
   check's own ABOUTME names both sides of it. Borrowed from modal-surfaces.test.mjs. */
const stripComments = (text) =>
  text.replace(/<!--[\s\S]*?-->/g, "").replace(/\/\*[\s\S]*?\*\//g, "");

/* The selector that owns the line at `index`, read back to the brace that opened it. */
function enclosingSelector(text, index) {
  const brace = text.lastIndexOf("{", index);
  if (brace === -1) return "";
  const start = Math.max(
    text.lastIndexOf("}", brace),
    text.lastIndexOf("{", brace - 1),
    text.lastIndexOf(";", brace)
  );
  return text.slice(start + 1, brace).trim();
}

function lineAt(text, index) {
  const end = text.indexOf("\n", index);
  return text.slice(text.lastIndexOf("\n", index) + 1, end === -1 ? undefined : end);
}

/* Two places a hue can be claimed for tools: the selector it is declared under (`.bar.tool`) and
   the line itself, for the flow's `tone === "tool" ? …` mapping, which is script and has no
   selector at all. The token names come out of the line before it is read, or `var(--tool)` would
   vouch for whatever selector it had been moved onto. `\btool\b` and not `tools?` on purpose —
   .allow-tools-icon is a control, not a span, and amber would be a legitimate choice there. */
const claimsTool = (text, index) =>
  /\btool\b/.test(enclosingSelector(text, index)) ||
  /\btool\b/.test(lineAt(text, index).replaceAll(/--(tool|warning)/g, ""));

const files = sourceFiles(root);
const sources = files.map((path) => ({
  short: path.slice(root.length),
  source: stripComments(readFileSync(path, "utf8"))
}));

describe("tool identity", () => {
  it("is looked for across every source in the console", () => {
    assert.ok(files.length > 20, `only ${files.length} sources found — is the scan root right?`);
  });

  it("is never painted with --warning", () => {
    for (const { short, source } of sources) {
      for (const hit of source.matchAll(/var\(--warning\)/g)) {
        assert.ok(
          !claimsTool(source, hit.index),
          `${short} paints tool identity with --warning near "${enclosingSelector(source, hit.index)}"` +
            ` — a tool is a category, --warning is an outcome; use --tool`
        );
      }
    }
  });

  it("still reaches the surfaces that draw tools, and only those", () => {
    let toolRefs = 0;

    for (const { short, source } of sources) {
      for (const hit of source.matchAll(/var\(--tool\)/g)) {
        /* The other direction, and the one a rename breaks silently: --tool is only carrying the
           category if it is still reaching the surfaces that draw tools. */
        assert.ok(
          claimsTool(source, hit.index),
          `${short} uses --tool away from anything tool-shaped, near ` +
            `"${enclosingSelector(source, hit.index)}" — that is a category hue on a stranger`
        );
        toolRefs += 1;
      }
    }

    /* Floors, or a console that renamed every .tool surface — or lost the tokens outright — would
       read as a clean split rather than an empty scan. */
    assert.ok(toolRefs >= 12, `expected the console's tool surfaces to read --tool, found ${toolRefs}`);
  });

  it("is a split, so app.css has to declare both halves", () => {
    const tokens = readFileSync(`${root}app.css`, "utf8");
    for (const token of ["--tool", "--warning"]) {
      assert.ok(
        new RegExp(`^\\s*${token}:`, "m").test(tokens),
        `app.css no longer declares ${token} — the split needs both halves to be a split`
      );
    }
  });
});
