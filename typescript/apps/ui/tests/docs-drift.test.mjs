// ABOUTME: Keeps the AGENTS.md files under typescript/ and this app's README pointing at things that
// exist: every relative link and backticked file path resolves, every `just <recipe>` is in the
// justfile, every `pnpm ... run <script>` is in the package.json it runs in, and every `//package`
// label names a Bazel package. A rename fails here instead of leaving the docs quietly wrong. The
// console's Setup pane runs each SDK's services with the commands its README's quickstart gives.

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, it } from "vitest";

import { FEATURES, SDKS } from "../src/lib/components/chat/HomeView.svelte";

const repo = new URL("../../../../", import.meta.url).pathname;
const docs = ["typescript/AGENTS.md", "typescript/apps/ui/AGENTS.md", "typescript/apps/ui/README.md"];
const recipes = new Set(
  [...readFileSync(resolve(repo, "justfile"), "utf8").matchAll(/^@?([a-z_][\w-]*)(?:\s[^:=\n]*)?:(?!=)/gm)].map(([, name]) => name)
);
const scripts = (dir) => Object.keys(JSON.parse(readFileSync(resolve(dir, "package.json"), "utf8")).scripts ?? {});

/** Paths in the docs are relative to the doc, or failing that to the repo root. */
const found = (doc, path) => [dirname(resolve(repo, doc)), repo].map((base) => resolve(base, path)).find(existsSync);

describe.each(docs)("%s", (doc) => {
  const text = readFileSync(resolve(repo, doc), "utf8");
  const code = [...text.matchAll(/```[^\n]*\n([\s\S]*?)```|`([^`\n]+)`/g)].map(([, block, span]) => block ?? span);

  it("links and names only files that exist", () => {
    const links = [...text.matchAll(/\]\(([^)\s#]+)(?:#[^)]*)?\)/g)].map(([, target]) => target).filter((target) => !/^[a-z]+:/.test(target));
    const paths = code.filter((span) => /^[\w.-]+(\/[\w.-]+)+\.(ts|tsx|mjs|js|svelte|css|md|json|yaml|sh|bazel)$/.test(span));
    assert.ok(links.length > 0 || paths.length > 0, "nothing to check — has the doc moved?");
    assert.deepEqual([...links, ...paths].filter((path) => !found(doc, path)), [], "paths that no longer exist: update the doc");
  });

  it("runs only recipes and scripts that exist", () => {
    const missing = [];
    for (const snippet of code) {
      for (const [, recipe] of snippet.matchAll(/\bjust ([a-z_][\w-]*)/g)) if (!recipes.has(recipe)) missing.push(`just ${recipe}`);
      for (const [command, dir = ".", script] of snippet.matchAll(/\bpnpm (?:-- )?(?:--dir "?(?:\$PWD\/)?([^\s"]+)"? )?run ([\w:-]+)/g)) {
        const pkg = found(doc, dir);
        if (!pkg || !scripts(pkg).includes(script)) missing.push(command);
      }
    }
    assert.deepEqual(missing, [], "commands with no matching justfile recipe or package.json script: update the doc");
  });

  it("names only Bazel packages that exist", () => {
    const packages = code.flatMap((snippet) =>
      [...snippet.matchAll(/(?<![\w:/])\/\/([\w./-]*)/g)].map(([, pkg]) => pkg.replace(/\/?\.\.\.$/, "").replace(/\/$/, ""))
    );
    assert.deepEqual(packages.filter((pkg) => !existsSync(resolve(repo, pkg, "BUILD.bazel"))).map((pkg) => `//${pkg}`), [], "Bazel packages that no longer exist: update the doc");
  });
});

/* The console's Setup pane tells a reader to start the three services, and the SDK's README is
   where those commands are maintained: a change to one without the other fails here. */
describe.each(SDKS)("the console's $label quickstart", (sdk) => {
  it("starts the three services with the README's commands, in its order", () => {
    const readme = readFileSync(resolve(repo, sdk.quickstart.replace(/^.*\/blob\/main\//, "").replace(/#.*$/, "")), "utf8");
    const section = readme.match(/### 3\. Start the three services\n([\s\S]*?)\n### /)?.[1];
    assert.ok(section, "the README has no \"### 3. Start the three services\" step: update the console's Setup pane with it");
    const commands = [...section.matchAll(/```bash\n([\s\S]*?)\n```/g)].map(([, block]) => block.trim());
    assert.deepEqual(sdk.services.map((service) => service.command), commands);
  });
});

/* A feature card links the guide section that explains it. GitHub makes the anchor from the heading,
   keeping an emoji's invisible joiners, so both are compared on their ASCII letters alone. */
describe("the console's feature cards", () => {
  const ascii = (text) => text.replace(/[^a-z0-9]+/gi, "").toLowerCase();
  it.each(FEATURES)("$title links a section its guide still has", ({ href }) => {
    const [path, anchor] = href.replace(/^.*\/blob\/main\//, "").split("#");
    const headings = [...readFileSync(resolve(repo, path), "utf8").matchAll(/^#{1,6} (.+)$/gm)].map(([, heading]) => ascii(heading));
    assert.ok(headings.includes(ascii(decodeURIComponent(anchor))), `${path} has no heading for #${anchor}: update the card's link`);
  });
});
