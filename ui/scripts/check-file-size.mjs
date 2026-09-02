/**
 * How big the source files under src/ are getting, largest first.
 *
 * Reported because nothing was counting. agentRun.svelte.ts went from 1,337
 * lines to 1,900 in a single day and the only reason anyone noticed was that it
 * looked long. The cost of that is not aesthetic: four agents edited it that
 * day and two collided, because a file wide enough to hold every concern is a
 * file everyone has to have open at once. A number printed on every run is what
 * turns that from a thing you notice afterwards into a thing you watch.
 *
 * Never fails, and that is structural rather than a soft opinion about size.
 * Every script here runs under one glob with `set -e`, so a size threshold that
 * exited non-zero would block every commit in the repo the moment a file
 * crossed it — on a refactor nobody scheduled, in a file the commit may not
 * even touch. It informs; it does not gate. `git diff --stat` is the tool for
 * arguing about a specific change; this is just the standing total.
 */
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const SOURCE = /\.(svelte|ts|css)$/;
/* Roughly where a file stops fitting in one head and starts being a merge
   surface. Marked, not enforced — see above. */
const NOTABLE_LINES = 1_000;
const SHOW = 12;

const root = process.argv[2] ?? new URL("../src", import.meta.url).pathname;

/* Anything that stops this reading the tree is reported and shrugged off: a
   missing directory or an unreadable file must not be the reason a commit
   fails, since this check has no opinion worth blocking on. */
let names = [];
try {
  names = await readdir(root, { recursive: true });
} catch (error) {
  console.log(`source size: nothing counted (${error.message})`);
  process.exit(0);
}

const files = [];
for (const name of names.filter((n) => SOURCE.test(n))) {
  try {
    const text = await readFile(join(root, name), "utf8");
    /* Newlines, so this agrees with `wc -l` — the number anyone checking this
       by hand will get. */
    files.push({ name, lines: (text.match(/\n/g) ?? []).length });
  } catch {
    // A file that cannot be read is one file missing from a total, not a failure.
  }
}

files.sort((a, b) => b.lines - a.lines);
const total = files.reduce((sum, file) => sum + file.lines, 0);
const width = String(files[0]?.lines ?? 0).length;

console.log(
  `source size: ${total.toLocaleString()} lines across ${files.length} files, largest first:`
);
for (const file of files.slice(0, SHOW)) {
  const mark = file.lines >= NOTABLE_LINES ? "  <-- over 1k" : "";
  console.log(`  ${String(file.lines).padStart(width)}  ${file.name}${mark}`);
}
const rest = files.length - SHOW;
if (rest > 0) {
  const tail = files.slice(SHOW).reduce((sum, file) => sum + file.lines, 0);
  console.log(`  ${String(tail).padStart(width)}  (${rest} smaller files)`);
}
