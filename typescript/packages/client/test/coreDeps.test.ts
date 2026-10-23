// The core SDK packages stay free of TanStack: it belongs in adapters and README recipes, so an app
// that never uses it never installs it.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

for (const pkg of ["client", "codegen", "react", "svelte"]) {
  test(`the core ${pkg} package does not depend on TanStack`, () => {
    const manifest = JSON.parse(readFileSync(`typescript/packages/${pkg}/package.json`, "utf8"));
    const deps = Object.keys({ ...manifest.dependencies, ...manifest.peerDependencies, ...manifest.devDependencies });
    assert.deepEqual(deps.filter((name) => name.startsWith("@tanstack/")), []);
  });
}
