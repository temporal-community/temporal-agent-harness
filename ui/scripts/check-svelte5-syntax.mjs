import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

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

let failed = false;
for (const file of await collect(root)) {
  const text = await readFile(file, "utf8");
  for (const rule of forbidden) {
    if (rule.pattern.test(text)) {
      console.error(`${file}: ${rule.name}`);
      failed = true;
    }
  }
}

if (failed) process.exit(1);
