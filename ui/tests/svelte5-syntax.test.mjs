// ABOUTME: Asserts that no Svelte 4 syntax survives anywhere in the component tree — every
// pattern below has a Svelte 5 replacement, and mixing the two is what makes a component
// silently fall out of runes mode.

import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, it } from "vitest";

const root = new URL("../src", import.meta.url).pathname;
const forbidden = [
  { name: "legacy event directive", pattern: /\bon:[a-zA-Z]/ },
  { name: "legacy prop export", pattern: /\bexport\s+let\b/ },
  { name: "legacy reactive label", pattern: /(^|\n)\s*\$:\s*/ },
  { name: "legacy slot element", pattern: /<slot\b/ },
  { name: "legacy event dispatcher", pattern: /\bcreateEventDispatcher\b/ },
  { name: "legacy $$props", pattern: /\$\$props\b/ },
  { name: "legacy $$restProps", pattern: /\$\$restProps\b/ },
  { name: "legacy dynamic component", pattern: /<svelte:component\b/ },
  { name: "legacy self component", pattern: /<svelte:self\b/ }
];

async function collect(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) files.push(...(await collect(path)));
    else if (entry.name.endsWith(".svelte")) files.push(path);
  }
  return files;
}

const files = await collect(root);

describe("Svelte 5 syntax", () => {
  /* An empty scan reports no legacy syntax just as cheerfully as a clean tree does, so the
     floor is what tells the two apart when the root moves. */
  it("is checked across the whole component tree", () => {
    assert.ok(files.length > 20, `only ${files.length} components found — is the scan root right?`);
  });

  it("has left no Svelte 4 syntax behind", async () => {
    const offenders = [];
    for (const file of files) {
      const text = await readFile(file, "utf8");
      for (const rule of forbidden) {
        if (rule.pattern.test(text)) offenders.push(`${file.slice(root.length + 1)}: ${rule.name}`);
      }
    }
    assert.deepEqual(offenders, [], "components still written in Svelte 4 syntax");
  });
});
