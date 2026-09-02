/**
 * Design-system drift check. Same shape as check-svelte5-syntax.mjs: one file,
 * no framework, reporting the five ways a hand-rolled control drifts away from
 * the primitives.
 *
 * Warn mode on purpose: the existing baseline is large, and a check that blocks
 * everyone on day one gets deleted rather than paid down. Report first, make it
 * fail once the count is heading the right way.
 */
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const root = process.argv[2] ?? new URL("../src", import.meta.url).pathname;

/* Primitives are the definition, so they are allowed to spell it out. */
const PRIMITIVES = /\/components\/primitives\//;

const CONTROL_HEIGHTS = new Set(["20px", "22px", "28px", "34px"]);

/* Classes worn by an <input>, <textarea> or <select> in this file's markup, so
   that a height on `.composer-input` is read the same way as one on
   `.composer input`. */
function formElementClasses(text) {
  const names = new Set();
  for (const tag of text.matchAll(/<(?:input|textarea|select)\b[^>]*>/g)) {
    for (const attr of tag[0].matchAll(/class=(?:"([^"]*)"|\{([^}]*)\})/g)) {
      for (const name of (attr[1] ?? attr[2]).matchAll(/[a-zA-Z][\w-]*/g)) names.add(name[0]);
    }
  }
  return names;
}

/* The selector of the rule a declaration sits in: back up to the `{` that
   opened it, then keep collecting the lines above it while they end in a comma. */
function enclosingSelector(lines, i) {
  for (let j = i; j >= 0 && i - j < 20; j -= 1) {
    const brace = lines[j].indexOf("{");
    if (brace === -1) continue;
    let selector = lines[j].slice(0, brace);
    for (let k = j - 1; k >= 0 && /,\s*$/.test(lines[k]); k -= 1) selector = `${lines[k]} ${selector}`;
    return selector;
  }
  return "";
}

/* A rule that dresses a form element. `cursor: pointer` finds buttons, and a
   text field has none — which is how the composer's literal 32px sat beside a
   28px send button unseen. A pseudo-element is exempt: a range input's
   ::-webkit-slider-thumb is 18px because the lane is, not because it missed
   the control scale. */
function formTarget(selector, formClasses) {
  if (selector.includes("::")) return false;
  if (/(?:^|[\s>+~(])(?:input|textarea|select)\b/.test(selector)) return true;
  return [...selector.matchAll(/\.([a-zA-Z][\w-]*)/g)].some((m) => formClasses.has(m[1]));
}

async function collect(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) files.push(...(await collect(path)));
    else if (entry.name.endsWith(".svelte") || entry.name.endsWith(".ts")) files.push(path);
  }
  return files;
}

const files = await collect(root);
const findings = { focus: [], height: [], hover: [], opacity: [], dead: [] };

for (const file of files.filter((f) => f.endsWith(".svelte"))) {
  const text = await readFile(file, "utf8");
  /* Comments blanked, newlines kept, for the same reason the dead-selector pass
     strips them: prose may not vouch for a rule. These heuristics all read a
     window of lines around a declaration, and the comment explaining that the
     height rule only looks near a `cursor: pointer` was itself enough to make
     the height rule think it had found a `cursor: pointer`. */
  const lines = text
    .replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "))
    .split("\n");
  const short = file.slice(file.indexOf("/src/") + 5);
  const formClasses = formElementClasses(text);

  lines.forEach((line, i) => {
    const at = `${short}:${i + 1}`;

    /* A focus ring removed and not replaced. The rule block is scanned rather
       than the line, because the replacement is usually a box-shadow two lines
       down. */
    if (/^\s*outline:\s*(0|none)\s*;/.test(line)) {
      const block = lines.slice(Math.max(0, i - 8), i + 6).join("\n");
      const targetsFocus = /:focus(-visible)?\b/.test(block);
      const replaced = /box-shadow:[^;]*(--focus-ring|0 0 0)/.test(block);
      if (targetsFocus && !replaced) findings.focus.push(at);
    }

    /* A control height off the 22/28/34 scale. Only sizes in control range are
       considered: a 9px pip and a 180px chart are not controls that missed the
       scale, they are something else measured in pixels. Two ways to be a
       control: a pointer cursor, or being a form element — app.css says nothing
       hard-codes a height outside the scale, and a text field is bound by that
       sentence even though nothing about it is clickable. */
    const height = line.match(/^\s*(?:min-)?height:\s*(\d+)px\s*;/);
    const px = height ? Number(height[1]) : 0;
    if (height && px >= 18 && px <= 40 && !CONTROL_HEIGHTS.has(`${px}px`) && !PRIMITIVES.test(file)) {
      const block = lines.slice(Math.max(0, i - 14), i + 14).join("\n");
      const selector = enclosingSelector(lines, i);
      if (/cursor: pointer/.test(block) || formTarget(selector, formClasses)) {
        findings.height.push(`${at}  ${px}px`);
      }
    }

    /* A disabled state with its own opacity instead of the token. */
    const opacity = line.match(/^\s*opacity:\s*(0\.\d+)\s*;/);
    if (opacity) {
      const block = lines.slice(Math.max(0, i - 4), i + 1).join("\n");
      if (/:disabled|\.disabled\b/.test(block)) findings.opacity.push(`${at}  ${opacity[1]}`);
    }
  });

  /* Hover styling with no pointer guard: on a touch screen the hover state
     sticks to whatever was tapped last.

     One finding per rule, at its line. It used to be one per file, which made a
     file with seven of them read as one thing to fix, and it took the file's
     first guard as vouching for every rule in it — so the number was smaller
     than the work in two different directions. Nesting is tracked instead: a
     rule is guarded only if a `hover: hover` at-rule is actually above it. */
  for (const block of text.match(/<style[\s\S]*?<\/style>/g) ?? []) {
    const offset = text.indexOf(block);
    /* Comments blanked but their newlines kept, so a `:hover` written in prose
       is not a rule and the reported lines still land. */
    const clean = block.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "));
    const open = [];
    let from = 0;
    for (const brace of clean.matchAll(/[{}]/g)) {
      if (brace[0] === "}") open.pop();
      else {
        const prelude = clean.slice(from, brace.index);
        open.push(prelude);
        if (/:hover\b/.test(prelude) && !open.some((p) => /@media[^{]*hover:\s*hover/.test(p))) {
          const start = offset + from + (prelude.length - prelude.trimStart().length);
          const line = text.slice(0, start).split("\n").length;
          findings.hover.push(`${short}:${line}  ${prelude.trim().replace(/\s+/g, " ")}`);
        }
      }
      from = brace.index + 1;
    }
  }
}

/* Dead :global() selectors. The compiler checks scoped selectors for us and
   emits css_unused_selector, but :global() is exactly the opt-out from that
   analysis — so reaching into a child component is both the only way to style
   it and the only way to lose the compiler's check. That is how `.badge`
   outlived Badge's migration to Chip, setting a shrink-and-ellipsis contract
   nothing had matched for weeks. */
const THIRD_PARTY = /^(svelte-flow|xyflow|tippy|cm-)/;

/* Everywhere a class can be PRODUCED: markup and script, with style blocks and
   comments removed. A rule may not vouch for itself, and neither may prose. */
const producible = (await Promise.all(files.map((f) => readFile(f, "utf8"))))
  .map((text) =>
    text
      .replace(/<style[\s\S]*?<\/style>/g, "")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/(^|[^:])\/\/[^\n]*/g, "$1")
  )
  .join("\n");

/* A class is alive if it is written out whole, or if it is the tail of a
   template literal: `md-syntax-${kind}` produces .md-syntax-string, and
   `edge-${kind}` produces .edge-main, neither of which appears literally.

   Where this stops: a name assembled the other way round — `${prefix}-row`, or
   two variables joined — is invisible to both passes, and working out what such
   an expression can evaluate to is constant propagation across functions, not a
   regex. Nothing in this tree needs it. The one class attribute that opens with
   an interpolation is `${activityLineClass(row)} activity-row-button`, and that
   function returns literals ("activity-line", "highlight-error") which the
   whole-word pass already sees. Left undone on purpose: a guess here shows up
   as a confident "delete this" on a live rule, which is the one way this check
   can do damage. */
const alive = (name) => {
  if (THIRD_PARTY.test(name)) return true;
  if (new RegExp(`["\`\\s.{(]${name}(?=["\`\\s.})])`).test(producible)) return true;
  const parts = name.split("-");
  for (let i = parts.length - 1; i > 0; i -= 1) {
    if (producible.includes(`${parts.slice(0, i).join("-")}-\${`)) return true;
  }
  return false;
};

for (const file of files.filter((f) => f.endsWith(".svelte"))) {
  const text = await readFile(file, "utf8");
  for (const block of text.match(/<style[\s\S]*?<\/style>/g) ?? []) {
    const offset = text.indexOf(block);
    for (const rule of block.matchAll(/:global\(([^)]*)\)/g)) {
      for (const cls of rule[1].matchAll(/\.([a-zA-Z][\w-]*)/g)) {
        if (alive(cls[1])) continue;
        const line = text.slice(0, offset + rule.index).split("\n").length;
        findings.dead.push(`${file.slice(file.indexOf("/src/") + 5)}:${line} :global(.${cls[1]})`);
      }
    }
  }
}

const report = [
  ["focus ring removed without a replacement", findings.focus],
  ["control height off the 22/28/34 scale", findings.height],
  ["hover styling with no (hover: hover) guard", findings.hover],
  ["disabled opacity instead of --disabled-opacity", findings.opacity],
  ["dead :global() selector, nothing can produce this class", findings.dead]
];

let total = 0;
for (const [name, hits] of report) {
  if (hits.length === 0) continue;
  total += hits.length;
  console.warn(`\n${name} (${hits.length}):`);
  for (const hit of hits) console.warn(`  ${hit}`);
}

if (total === 0) console.log("design system: clean");
else console.warn(`\ndesign system: ${total} finding(s), not failing the build yet`);
