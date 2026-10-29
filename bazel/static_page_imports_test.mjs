// static_page_imports_test.mjs <directory>: every module a static_page target's page loads
// (bazel/static_page.bzl) is in its directory. Each local path index.html's import map names is
// there, each relative import resolves to a file there, and each bare import is covered by the
// import map.

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

const [directory] = process.argv.slice(2);

/** Every file under dir, by path relative to it. In runfiles every file is a symlink, so anything
 * that is not a directory counts as a file. */
function walk(dir, prefix = "") {
  return readdirSync(join(dir, prefix), { withFileTypes: true }).flatMap((entry) => {
    const path = prefix ? `${prefix}/${entry.name}` : entry.name;
    return entry.isDirectory() ? walk(dir, path) : [path];
  });
}

const files = new Set(walk(directory));
const read = (path) => readFileSync(join(directory, path), "utf8");
/** The bodies of the <script> elements in html, only those of the given `type` if one is given.
 * HTML ignores case in tag and attribute names and allows anything before an end tag's `>`. */
const scripts = (html, type) =>
  [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\b[^>]*>/gi)]
    .filter(([, attrs]) => !type || new RegExp(`\\btype\\s*=\\s*["']?${type}["'\\s]`, "i").test(`${attrs} `))
    .map(([, , body]) => body);

const html = read("index.html");
const importMaps = scripts(html, "importmap");
assert.equal(importMaps.length, 1, "index.html needs exactly one import map");
const { imports } = JSON.parse(importMaps[0]);

/** A path relative to the page's root, or undefined for a URL with a scheme. */
const local = (url, base) => {
  if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return undefined;
  return new URL(url, `file:///${base}`).pathname.slice(1);
};

/** What a module specifier imported from `from` (a path in the page) loads: a path in the page,
 * undefined for a URL, or null when nothing maps it. */
function resolveSpecifier(specifier, from) {
  if (/^\.{0,2}\//.test(specifier)) return local(specifier, from);
  if (/^[a-z][a-z0-9+.-]*:/i.test(specifier)) return undefined;
  if (specifier in imports) return local(imports[specifier], "index.html");
  const prefix = Object.keys(imports)
    .filter((key) => key.endsWith("/") && specifier.startsWith(key))
    .sort((a, b) => b.length - a.length)[0];
  return prefix ? local(imports[prefix] + specifier.slice(prefix.length), "index.html") : null;
}

/** The specifiers of a module's run-time imports: static, side-effect, re-exports and dynamic.
 * Type-only imports compile away, so they are left out. */
function specifiers(source) {
  const found = [];
  const statement = /^\s*(import|export)\s+(type\s+)?([^;]*?)\bfrom\s*["']([^"']+)["']/gm;
  for (const [, , typeOnly, , specifier] of source.matchAll(statement)) if (!typeOnly) found.push(specifier);
  for (const [, specifier] of source.matchAll(/^\s*import\s*["']([^"']+)["']/gm)) found.push(specifier);
  for (const [, specifier] of source.matchAll(/\bimport\s*\(\s*["']([^"']+)["']\s*\)/g)) found.push(specifier);
  return found;
}

/** Each module in the page and its source: index.html's inline module scripts, every .js file and
 * every .svelte component's scripts. */
function modules() {
  const found = scripts(html, "module").map((source) => ["index.html", source]);
  for (const path of files) {
    if (path.endsWith(".js")) found.push([path, read(path)]);
    else if (path.endsWith(".svelte")) found.push(...scripts(read(path)).map((source) => [path, source]));
  }
  return found;
}

test("every local path the import map names is in the page", () => {
  const missing = Object.entries(imports).filter(([key, url]) => {
    const path = local(url, "index.html");
    if (path === undefined) return false;
    return key.endsWith("/") ? ![...files].some((file) => file.startsWith(path)) : !files.has(path);
  });
  assert.deepEqual(missing, [], "import map entries with nothing in the page");
});

test("every import in the page resolves to a file in it or a URL", () => {
  const unresolved = [];
  for (const [path, source] of modules()) {
    for (const specifier of specifiers(source)) {
      const target = resolveSpecifier(specifier, path);
      if (target === null) unresolved.push(`${path}: ${specifier} (not in the import map)`);
      else if (target !== undefined && !files.has(target)) unresolved.push(`${path}: ${specifier} (no ${target})`);
    }
  }
  assert.deepEqual(unresolved, [], "imports the browser cannot load");
});
