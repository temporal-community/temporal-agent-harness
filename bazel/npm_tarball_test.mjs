// npm_tarball_test.mjs <tarball> <package.json>: the tarball a published_npm_package target builds
// (bazel/npm_package.bzl) holds what its package.json says it does. Every path package.json names
// is in it, everything in it is covered by `files`, its relative imports resolve inside it, and it
// ships no TypeScript sources or tests.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { gunzipSync } from "node:zlib";

const [tarballPath, packageJsonPath] = process.argv.slice(2);

/** The regular files in a tarball, by path under `package/`, as text. */
function readTarball(path) {
  const archive = gunzipSync(readFileSync(path));
  const files = new Map();
  for (let offset = 0; offset + 512 <= archive.length; ) {
    const header = archive.subarray(offset, offset + 512);
    if (header.every((byte) => byte === 0)) break;
    const field = (start, length) => header.subarray(start, start + length).toString("utf8").replace(/\0.*$/s, "");
    const prefix = field(345, 155);
    const name = prefix ? `${prefix}/${field(0, 100)}` : field(0, 100);
    const size = parseInt(field(124, 12).trim() || "0", 8);
    const type = field(156, 1);
    if (type === "0" || type === "") {
      const body = archive.subarray(offset + 512, offset + 512 + size).toString("utf8");
      files.set(name.replace(/^package\//, ""), body);
    }
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  return files;
}

/** Every path string under package.json's `exports`, `main`, `module`, `types`, `typings`, `bin`. */
function namedPaths(manifest) {
  const paths = [];
  const collect = (value) => {
    if (typeof value === "string") paths.push(value);
    else if (value && typeof value === "object") Object.values(value).forEach(collect);
  };
  for (const field of ["exports", "main", "module", "types", "typings", "bin"]) collect(manifest[field]);
  return paths.map((path) => path.replace(/^\.\//, ""));
}

const files = readTarball(tarballPath);
const manifest = JSON.parse(readFileSync(packageJsonPath, "utf8"));

test("the tarball's package.json is the package's", () => {
  assert(files.has("package.json"), "no package.json in the tarball");
  assert.deepEqual(JSON.parse(files.get("package.json")), manifest);
});

test("every path package.json names is in the tarball", () => {
  const missing = namedPaths(manifest).filter((path) => !files.has(path));
  assert.deepEqual(missing, [], "package.json names files the tarball does not have");
});

test("everything in the tarball is covered by package.json's files", () => {
  // npm always packs these, whatever `files` says.
  const always = /^(package\.json|README(\..*)?|LICEN[CS]E(\..*)?)$/i;
  const covered = (path) =>
    (manifest.files ?? []).some((entry) => path === entry || path.startsWith(`${entry.replace(/\/$/, "")}/`));
  const strays = [...files.keys()].filter((path) => !always.test(path) && !covered(path));
  assert.deepEqual(strays, [], "the tarball has files package.json's `files` does not list");
});

test("the tarball ships no TypeScript sources or tests", () => {
  const unwanted = [...files.keys()].filter(
    (path) => (/\.(ts|tsx|mts|cts)$/.test(path) && !/\.d\.[mc]?ts$/.test(path)) || /(^|\/)test\/|\.test\./.test(path),
  );
  assert.deepEqual(unwanted, [], "sources or tests in the tarball");
});

test("every relative import in the tarball resolves inside it", () => {
  const specifier = /(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)["'](\.{1,2}\/[^"']+)["']/g;
  const unresolved = [];
  for (const [path, body] of files) {
    if (!/\.(js|mjs|cjs|d\.ts)$/.test(path)) continue;
    const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    for (const [, relative] of body.matchAll(specifier)) {
      const target = new URL(relative, `file:///${dir ? `${dir}/` : ""}`).pathname.slice(1);
      // A declaration's `./x.js` or `./x.ts` (tsc keeps a source's `.ts` imports) names the
      // module whose types are in `./x.d.ts`.
      const candidates = path.endsWith(".d.ts") ? [target, target.replace(/\.(js|ts)$/, ".d.ts")] : [target];
      if (!candidates.some((candidate) => files.has(candidate))) unresolved.push(`${path}: ${relative}`);
    }
  }
  assert.deepEqual(unresolved, [], "relative imports with no file in the tarball");
});
