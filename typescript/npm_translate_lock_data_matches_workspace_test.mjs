// npm_translate_lock's `data` in MODULE.bazel must list every workspace package's package.json, or
// Bazel stops updating pnpm-lock.yaml when that package changes. The lockfile's importers are the
// workspace's packages (pnpm keeps them in step with pnpm-workspace.yaml), so this compares the two.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

function lockfileImporters(lockfile) {
  // pnpm writes its own pin as a first YAML document; the workspace's packages are in the last.
  const workspace = lockfile.split(/^---\n/m).at(-1);
  const section = /^importers:\n((?: .*\n|\n)*)/m.exec(workspace);
  assert(section, "pnpm-lock.yaml has no importers section");
  // `  <path>:`, or `  <path>: {}` for a package with no dependencies.
  return [...section[1].matchAll(/^ {2}(\S[^:]*):(?: \{\})?$/gm)].map((match) =>
    match[1].replace(/^'(.*)'$/, "$1"),
  );
}

function translateLockData(moduleBazel) {
  const call = /npm\.npm_translate_lock\(([\s\S]*?)\n\)/.exec(moduleBazel);
  assert(call, "MODULE.bazel has no npm.npm_translate_lock call");
  const data = /\bdata = \[([\s\S]*?)\]/.exec(call[1]);
  assert(data, "npm_translate_lock has no data list");
  return [...data[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]);
}

test("npm_translate_lock data lists pnpm-workspace.yaml and every workspace package.json", () => {
  const importers = lockfileImporters(readFileSync("typescript/pnpm-lock.yaml", "utf8"));
  const expected = [
    "//typescript:pnpm-workspace.yaml",
    ...importers.map((path) => (path === "." ? "//typescript:package.json" : `//typescript/${path}:package.json`)),
  ].sort();
  const actual = translateLockData(readFileSync("MODULE.bazel", "utf8")).sort();
  assert.deepEqual(actual, expected, "update npm_translate_lock's data in MODULE.bazel");
});
