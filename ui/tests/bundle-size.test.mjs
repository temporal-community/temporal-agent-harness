// ABOUTME: Guards the size of the JavaScript `just server` serves, read from the committed dist,
// so an accidental dependency (a second markdown pipeline, every highlighter language) fails here
// with the new number instead of shipping. Raise the budget on purpose, with the measurement.

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { it } from "vitest";

/* Measured 2026-10-09 with gzip -9: main served 747 KB / 241 KB; with TanStack Markdown, remend
   and @tanstack/highlight it is 814 KB / 266 KB. The budget is that plus about 4%, too little for
   a second markdown or highlighting pipeline to slip in. */
const BUDGET = { bytes: 847_000, gzip: 277_000 };
const assets = new URL("../../temporal_agent_harness/ui/dist/assets/", import.meta.url);

it("keeps the served JavaScript within its size budget", () => {
  const scripts = readdirSync(assets)
    .filter((name) => name.endsWith(".js"))
    .map((name) => readFileSync(new URL(name, assets)));
  assert.ok(scripts.length > 0, "dist has no JavaScript; run `just app-build`");
  const bytes = scripts.reduce((total, script) => total + script.length, 0);
  const gzip = scripts.reduce((total, script) => total + gzipSync(script, { level: 9 }).length, 0);
  assert.ok(bytes <= BUDGET.bytes, `served JS is ${bytes.toLocaleString()} bytes, over the ${BUDGET.bytes.toLocaleString()} budget`);
  assert.ok(gzip <= BUDGET.gzip, `served JS is ${gzip.toLocaleString()} bytes gzipped, over the ${BUDGET.gzip.toLocaleString()} budget`);
});
