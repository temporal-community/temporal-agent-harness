// ABOUTME: Asserts that every modal surface in the console is a real <dialog> opened with
// showModal(), because the alternative is a claim without the thing it claims. The help sheet used
// to be a <div role="dialog" aria-modal="true"> inside a hand-rolled .scrim: aria-modal tells a
// screen reader that nothing outside the surface matters, while Tab walked straight out of it into
// the app behind. showModal() is what actually delivers that promise — the top layer brings focus
// containment, Escape, focus restore on close, and a real ::backdrop, none of which the `open`
// attribute gives you. So the rules below are all one rule from different angles: don't describe a
// modal, be one.

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it } from "vitest";

const root = fileURLToPath(new URL("../src/lib/", import.meta.url));

function svelteFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = `${dir}${entry.name}`;
    if (entry.isDirectory()) return svelteFiles(`${path}/`);
    return entry.name.endsWith(".svelte") ? [path] : [];
  });
}

/* Comments are prose about markup, not markup. This check's own ABOUTME and the note in
   HotkeyHelp both spell `aria-modal` out in order to explain why it is gone, and a scanner that
   cannot tell those apart is a scanner nobody can write a comment near. */
const stripComments = (text) =>
  text.replace(/<!--[\s\S]*?-->/g, "").replace(/\/\*[\s\S]*?\*\//g, "");

/* The selector that owns the line at `index`, read back to the brace that opened it. */
function enclosingSelector(text, index) {
  const brace = text.lastIndexOf("{", index);
  if (brace === -1) return "";
  const start = Math.max(
    text.lastIndexOf("}", brace),
    text.lastIndexOf("{", brace - 1),
    text.lastIndexOf(";", brace)
  );
  return text.slice(start + 1, brace).trim();
}

const files = svelteFiles(root);
const components = files.map((path) => ({
  short: path.slice(root.length),
  source: stripComments(readFileSync(path, "utf8"))
}));

describe("modal surfaces", () => {
  it("are scanned from the whole component tree", () => {
    assert.ok(files.length > 20, `only ${files.length} components found — is the scan root right?`);
  });

  it("are never impersonated by an element that says it is one", () => {
    for (const { short, source } of components) {
      /* 1. Nobody hand-writes aria-modal. showModal() sets it, and writing it by hand is exactly the
         tell that a div is impersonating a dialog. */
      assert.ok(
        !/aria-modal/.test(source),
        `${short} hand-writes aria-modal — use a <dialog> with showModal(), which sets it for you`
      );

      /* 2. role="dialog" is the same impersonation with a different attribute. */
      assert.ok(
        !/role=["']dialog["']/.test(source),
        `${short} declares role="dialog" on an element — use a real <dialog>`
      );
    }
  });

  it("are opened with showModal(), never with the open attribute", () => {
    let dialogs = 0;

    for (const { short, source } of components) {
      for (const tag of source.matchAll(/<dialog\b[^>]*>/g)) {
        dialogs += 1;

        /* 3. The `open` attribute renders a dialog with none of the behaviour that makes it one: no
           top layer, no focus containment, no ::backdrop. It is the trap this whole check exists to
           keep shut, because `<dialog open>` looks correct and behaves like the div it replaced. */
        assert.ok(
          !/\sopen(?=[\s>=])/.test(tag[0]),
          `${short} opens a dialog with the open attribute — call showModal() instead`
        );

        /* 4. And it has to actually be opened somewhere in the file that renders it. */
        assert.ok(
          source.includes("showModal("),
          `${short} renders a <dialog> but never calls showModal()`
        );
      }
    }

    /* Without a floor this file passes just as happily on a console with no modals in it at all, so a
       renamed component or a moved scan root would read as success. */
    assert.ok(
      dialogs >= 2,
      `expected the node inspector and the help sheet, found ${dialogs} dialog(s)`
    );
  });

  it("paint their scrim on ::backdrop and nowhere else", () => {
    let backdrops = 0;

    for (const { short, source } of components) {
      /* 5. The scrim is the dialog's ::backdrop. Painting the scrim token on an ordinary element is
         how the hand-rolled overlay comes back: it puts a node between the app and the surface, which
         then needs its own click handler, its own Escape, and its own z-index. */
      for (const hit of source.matchAll(/--overlay-scrim/g)) {
        const selector = enclosingSelector(source, hit.index);
        if (!selector) continue;
        assert.ok(
          selector.includes("::backdrop"),
          `${short} paints --overlay-scrim on "${selector}" — the scrim belongs on ::backdrop`
        );
        backdrops += 1;
      }
    }

    assert.ok(backdrops >= 2, `expected both dialogs to paint a ::backdrop, found ${backdrops}`);
  });
});
