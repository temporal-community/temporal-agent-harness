// ABOUTME: Pins that a `data-tip` bubble shows for focus the reader moved, not focus code handed
// back. Picking a session with Enter returned focus to the drawer icon, `:focus-visible` matched
// because the last input was a key, and "Open Session Manager" sat over the chat header until
// something else took focus. Tab and the arrows still show it; blur resets the element.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { quietProgrammaticTips } from "./tipFocus.ts";

function fakeDocument() {
  const handlers = {};
  return {
    handlers,
    addEventListener: (type, handler) => (handlers[type] = handler),
    removeEventListener: (type) => delete handlers[type]
  };
}

function element(tip = true) {
  const attributes = new Map(tip ? [["data-tip", "Open Session Manager"]] : []);
  return {
    attributes,
    hasAttribute: (name) => attributes.has(name),
    setAttribute: (name, value) => attributes.set(name, value),
    removeAttribute: (name) => attributes.delete(name)
  };
}

describe("tips shown for focus", () => {
  it("quiets a tip that code focused after Enter, and clears it on blur", () => {
    const doc = fakeDocument();
    quietProgrammaticTips(doc);
    const icon = element();

    doc.handlers.keydown({ key: "Enter" });
    doc.handlers.focusin({ target: icon });
    assert.ok(icon.hasAttribute("data-tip-quiet"), "focus handed back after Enter is quiet");

    doc.handlers.focusout({ target: icon });
    assert.ok(!icon.hasAttribute("data-tip-quiet"));
  });

  it("shows it for focus reached with Tab or an arrow", () => {
    const doc = fakeDocument();
    quietProgrammaticTips(doc);
    for (const key of ["Tab", "ArrowRight"]) {
      const control = element();
      doc.handlers.keydown({ key });
      doc.handlers.focusin({ target: control });
      assert.ok(!control.hasAttribute("data-tip-quiet"), key);
    }
  });

  it("leaves elements without a tip alone, and uninstalls cleanly", () => {
    const doc = fakeDocument();
    const uninstall = quietProgrammaticTips(doc);
    const input = element(false);
    doc.handlers.focusin({ target: input });
    assert.ok(!input.hasAttribute("data-tip-quiet"));
    uninstall();
    assert.deepEqual(Object.keys(doc.handlers), []);
  });

  it("is installed at startup and hides a quiet tip unless it is hovered", () => {
    const main = readFileSync(new URL("../../../main.ts", import.meta.url), "utf8");
    assert.match(main, /quietProgrammaticTips\(document\);/);
    const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    assert.match(
      css,
      /\[data-tip\]\[data-tip-quiet\]:not\(:hover\)::before,\s*\[data-tip\]\[data-tip-quiet\]:not\(:hover\)::after \{\s*opacity: 0;/
    );
  });

  /* Clicking the session chip opened the drawer under a still-hovered chip, and its tip sat
     over the drawer's own tabs. */
  it("hides the tip of a control whose menu or drawer is open", () => {
    const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    assert.match(
      css,
      /\[data-tip\]\[aria-expanded="true"\]::before,\s*\[data-tip\]\[aria-expanded="true"\]::after \{\s*opacity: 0;/
    );
  });
});
