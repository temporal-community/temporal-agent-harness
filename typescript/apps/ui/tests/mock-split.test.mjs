// ABOUTME: Keeps the `?mock=` fixtures out of the served bundle. Their top-level fixture data is not
// provably pure, so any static import path from main.ts ships it even when the code using it is
// dev-only; main.ts must reach them through import() alone.

import assert from "node:assert/strict";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { it } from "vitest";

const src = new URL("../src", import.meta.url).pathname;
/* Static `import … from`, bare `import "…"` and `export … from`; `import type` is erased, `import()` is lazy. */
const STATIC = /(?:^|[\n;])\s*(?:import(?!\s+type\b)(?:[^"'();]*?\bfrom)?|export\s[^"'();]*?\bfrom)\s*["']([^"']+)["']/g;

function resolve(spec, from) {
  const base = spec.startsWith("$lib/") ? join(src, "lib", spec.slice(5)) : spec.startsWith(".") ? join(dirname(from), spec) : null;
  if (!base) return null;
  return [base, `${base}.ts`, `${base}.svelte.ts`, join(base, "index.ts")].find((path) => existsSync(path) && statSync(path).isFile()) ?? null;
}

function staticGraph(entry) {
  const seen = new Set();
  const visit = (file) => {
    if (seen.has(file) || !/\.(ts|svelte)$/.test(file)) return;
    seen.add(file);
    for (const [, spec] of readFileSync(file, "utf8").matchAll(STATIC)) {
      const next = resolve(spec, file);
      if (next) visit(next);
    }
  };
  visit(entry);
  return [...seen].map((file) => relative(src, file));
}

it("reaches no mock fixture from main.ts through a static import", () => {
  const graph = staticGraph(join(src, "main.ts"));
  assert.ok(graph.includes("lib/state/agentRun.svelte.ts"), "the graph walk lost the app; fix the resolver");
  assert.deepEqual(graph.filter((file) => file.startsWith("lib/mock/") || file === "lib/api/mockClient.ts"), []);
});
