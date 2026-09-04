// ABOUTME: Asserts every ui/tools/ path named by a tracked file is itself tracked. Checks in here
// cite the browser probe that measured a claim they can only assert indirectly, which is good
// practice right up until the probe is deleted or ignored — at which point the citation says
// "measured in <file>" about a file no clone has, and the reader cannot tell whether the claim was
// ever true. Three of nine citations had already rotted that way when this was written. Existing on
// the author's disk is not the bar, because ui/tools/ is ignored by default: the bar is tracked, so
// this greps the paths and asks git, which is also what keeps the ignore file's exception list
// honest without anyone having to remember it.
//   node ui/scripts/check-tool-citations.mjs

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const repo = new URL("../../", import.meta.url).pathname;
const git = (...args) => execFileSync("git", args, { cwd: repo, encoding: "utf8" });

const tracked = new Set(git("ls-files").split("\n").filter(Boolean));
assert.ok(tracked.size > 100, "sanity: git listed the tree");

/* Cited paths, from the tracked tree only — an ignored probe citing another ignored probe is
   two scratch files agreeing with each other, which costs nobody anything. */
const cited = new Map();
for (const line of git("grep", "-hoE", "ui/tools/[A-Za-z0-9_./-]+").split("\n")) {
  const path = line.trim().replace(/[.,)]+$/, "");
  if (!path) continue;
  /* A directory is cited as a group ("codebadge-shots/"), and is satisfied by anything in it. */
  const isDir = path.endsWith("/");
  const ok = isDir
    ? [...tracked].some((file) => file.startsWith(path))
    : tracked.has(path) || [...tracked].some((file) => file.startsWith(`${path}/`));
  cited.set(path, ok);
}
assert.ok(cited.size > 0, "sanity: found citations to check");

const rotted = [...cited].filter(([, ok]) => !ok).map(([path]) => path);
assert.deepEqual(
  rotted,
  [],
  `a tracked file cites ui/tools/ paths that are not in the repo, so they do not exist for ` +
    `anyone else: ${rotted.join(", ")}. Either track the file (add an exception in ui/.gitignore) ` +
    `or drop the path from the comment and keep the finding.`
);

/* The same rule one step harder, because a citation only misleads a reader while an import
   breaks the run: verify-drawer-settle.mjs was tracked with `import { open } from
   "./scroll-probe/cdp.mjs"` while cdp.mjs was not, so the one harness in here anybody else
   could have run was the one thing they could not. Relative, so the grep above cannot see it. */
const brokenImports = [];
for (const file of [...tracked].filter((f) => f.startsWith("ui/tools/") && /\.(mjs|js|ts)$/.test(f))) {
  const source = readFileSync(new URL(file, new URL(repo, "file:")), "utf8");
  for (const [, specifier] of source.matchAll(/from\s+["'](\.[^"']+)["']/g)) {
    const resolved = new URL(specifier, new URL(file, "file:///")).pathname.slice(1);
    if (!tracked.has(resolved)) brokenImports.push(`${file} -> ${specifier}`);
  }
}
assert.deepEqual(
  brokenImports,
  [],
  `a tracked file under ui/tools/ imports something that is not tracked, so it cannot run from ` +
    `a fresh clone: ${brokenImports.join(", ")}`
);

console.log(
  `check-tool-citations: ${cited.size} cited ui/tools/ paths are tracked, and every tracked ` +
    `tool's own imports are too`
);
