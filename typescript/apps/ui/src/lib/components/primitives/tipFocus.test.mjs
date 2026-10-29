// ABOUTME: Pins that a `data-tip` bubble shows for focus the reader moved, not focus code handed
// back. Picking a session with Enter returned focus to the drawer icon, `:focus-visible` matched
// because the last input was a key, and "Open Session Manager" sat over the chat header until
// something else took focus. Tab and the arrows still show it; blur resets the element.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { TIP_MARGIN, keepTipsInView, quietProgrammaticTips, tipHeight } from "./tipFocus.ts";

function fakeDocument() {
  const handlers = {};
  return {
    handlers,
    addEventListener: (type, handler) => (handlers[type] = handler),
    removeEventListener: (type) => delete handlers[type]
  };
}

const visible = { overflowX: "visible", overflowY: "visible" };

function element(tip = true) {
  const attributes = new Map(tip ? [["data-tip", "Open Session Manager"]] : []);
  return {
    attributes,
    hasAttribute: (name) => attributes.has(name),
    getAttribute: (name) => attributes.get(name) ?? null,
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
      /\[data-tip\]\[data-tip-quiet\]:not\(:hover\)::before,\s*\[data-tip\]\[data-tip-quiet\]:not\(:hover\)::after,\s*\[data-tip\]\[data-tip-quiet\]:not\(:hover\) > \.tip-card \{\s*display: none;/
    );
  });

  /* Clicking the session chip opened the drawer under a still-hovered chip, and its tip sat
     over the drawer's own tabs. */
  it("hides the tip of a control whose menu or drawer is open", () => {
    const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    assert.match(
      css,
      /\[data-tip\]\[aria-expanded="true"\]::before,\s*\[data-tip\]\[aria-expanded="true"\]::after,\s*\[data-tip\]:active > \.tip-card,\s*\[data-tip\]\[aria-expanded="true"\] > \.tip-card \{\s*display: none;/
    );
  });

  /* At 420px the Web UI link's centred tip hung 12px past the window's right edge and widened
     the page. The room to each edge is measured as the tip shows, and the centring clamps to it. */
  it("holds a centred tip inside the window, from whichever edge is near", () => {
    const doc = fakeDocument();
    const uninstall = keepTipsInView(doc, { innerWidth: 420, innerHeight: 800, getComputedStyle: () => visible });
    const vars = new Map();
    const chip = {
      ...element(),
      parentElement: null,
      toggleAttribute: () => {},
      getBoundingClientRect: () => ({ left: 281, right: 312, top: 400, bottom: 428 }),
      style: { setProperty: (name, value) => vars.set(name, value) }
    };
    const icon = { closest: (selector) => (selector === "[data-tip]" ? chip : null) };
    doc.handlers.pointerover({ target: icon });
    assert.equal(vars.get("--tip-room-left"), `${296.5 - TIP_MARGIN}px`);
    assert.equal(vars.get("--tip-room-right"), `${420 - TIP_MARGIN - 296.5}px`);
    vars.clear();
    doc.handlers.focusin({ target: icon });
    assert.ok(vars.has("--tip-room-right"), "focus shows a tip too");
    uninstall();
    assert.deepEqual(Object.keys(doc.handlers), []);

    const main = readFileSync(new URL("../../../main.ts", import.meta.url), "utf8");
    assert.match(main, /keepTipsInView\(document\);/);
    const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    assert.match(
      css,
      /--tip-x: clamp\(\s*calc\(var\(--tip-room-left, 100vw\) \* -1\),\s*-50%,\s*calc\(var\(--tip-room-right, 100vw\) - 100%\)\s*\);/
    );
    assert.match(
      css,
      /box-sizing: border-box;[^}]*max-width: min\(320px, 60vw, calc\(100cqi - 16px\)\);/,
      "a bubble, padding and all, never needs more than the window, or the container that clips it"
    );
    /* The Session Manager's end control names its session, title and all. */
    assert.match(css, /overflow-wrap: anywhere;\s*max-height: calc\(8 \* 1\.4em \+ 8px\);\s*overflow: hidden;/);
  });

  /* A start-aligned bubble on a 403px session row sat 41.5px left of the row's centre, where its
     caret was; on a short tip the caret hung past the bubble's end. The bubble's hairline, painted
     over the caret's base, made the caret read as a fainter piece behind it. */
  it("centres every bubble on its caret, and draws the caret as the bubble's own solid fill", () => {
    const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    assert.doesNotMatch(css, /\[data-tip-align[^\]]*\][^{]*\{/, "no tip grows from its element's edge, away from its caret");
    assert.match(css, /\n\[data-tip\]::after,\n\[data-tip\] > \.tip-card \{\s*left: 50%;[^}]*transform: translate\(var\(--tip-x\), 0\);/);
    const caret = css.match(/\n\[data-tip\]::before \{([^}]*)\}/)?.[1] ?? "";
    assert.match(caret, /z-index: 41;/, "above the bubble's z-index 40, so nothing the bubble paints crosses it");
    assert.match(caret, /left: 50%;[^}]*transform: translate\(-50%, 0\);/, "on the element's centre");
    assert.match(caret, /border-top-color: var\(--surface-3\);/);
    const surface = css.match(/\n\[data-tip\]::after,\n\.tip-card \{([^}]*)\}/)?.[1] ?? "";
    assert.match(surface, /background: var\(--surface-3\);/, "the caret's colour is the bubble's");
    /* The caret's 5px triangle spans 8–13px from the element, the bubble starts at 12: 1px of overlap, no seam. */
    assert.match(caret, /bottom: calc\(100% \+ 3px\);\s*border: 5px solid transparent;/);
    assert.match(css, /\n\[data-tip\]::after,\n\[data-tip\] > \.tip-card \{\s*left: 50%;\s*bottom: calc\(100% \+ 12px\);/);
  });

  /* The attention queue clips its card's sliding rows and scrolls a tall card, so the reason
     button's tip in the card's top corner was cut off by the card. Whatever clips is the box. */
  it("keeps a tip inside the box its ancestors clip it to, flipping when its side has no room", () => {
    const doc = fakeDocument();
    const styles = new Map();
    const node = (rect, overflowX = "visible", overflowY = overflowX, parentElement = null) => {
      const it = { parentElement, getBoundingClientRect: () => rect };
      styles.set(it, { overflowX, overflowY });
      return it;
    };
    const body = node({ left: 0, right: 1440, top: 0, bottom: 900 });
    const queue = node({ left: 990, right: 1430, top: 560, bottom: 900 }, "auto", "auto", body);
    const viewport = node({ left: 1000, right: 1420, top: 640, bottom: 900 }, "clip", "clip", queue);
    const head = node({ left: 1012, right: 1408, top: 652, bottom: 680 }, "visible", "visible", viewport);
    keepTipsInView(doc, { innerWidth: 1440, innerHeight: 900, getComputedStyle: (n) => styles.get(n) });

    const tip = (rect, attributes = []) => {
      const vars = new Map();
      const on = new Set(attributes);
      const button = {
        parentElement: head,
        getAttribute: () => "Reject with a reason",
        hasAttribute: (name) => on.has(name),
        toggleAttribute: (name, force) => (force ? on.add(name) : on.delete(name)),
        getBoundingClientRect: () => rect,
        style: { setProperty: (name, value) => vars.set(name, value) }
      };
      doc.handlers.pointerover({ target: { closest: () => button } });
      return { vars, flipped: on.has("data-tip-flip") };
    };

    const corner = tip({ left: 1380, right: 1404, top: 654, bottom: 678 });
    assert.ok(corner.flipped, "14px under the card's clipped top: the tip shows below instead");
    assert.equal(corner.vars.get("--tip-room-right"), `${1420 - TIP_MARGIN - 1392}px`, "slides in from the card's edge, not the window's");
    assert.equal(corner.vars.get("--tip-room-left"), `${1392 - 1000 - TIP_MARGIN}px`);
    assert.ok(!tip({ left: 1380, right: 1404, top: 760, bottom: 784 }).flipped, "room above: it stays");
    assert.ok(tip({ left: 1380, right: 1404, top: 862, bottom: 886 }, ["data-tip-below"]).flipped, "a below tip at the foot goes above");
    assert.ok(!tip({ left: 1380, right: 1404, top: 760, bottom: 784 }, ["data-tip-flip"]).flipped, "a flip left from before clears");

    const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    assert.match(css, /\[data-tip-below\],\s*\[data-tip\]\[data-tip-flip\] \{\s*--tip-dir: -1;/);
    assert.match(css, /\[data-tip-below\]\[data-tip-flip\] \{\s*--tip-dir: 1;/);
    assert.match(css, /\[data-tip-below\]:not\(\[data-tip-flip\]\)::after,\s*\[data-tip\]\[data-tip-flip\]:not\(\[data-tip-below\]\)::after,\s*\[data-tip-below\]:not\(\[data-tip-flip\]\) > \.tip-card,\s*\[data-tip\]\[data-tip-flip\]:not\(\[data-tip-below\]\) > \.tip-card \{\s*bottom: auto;\s*top: calc\(100% \+ 12px\);/);
    assert.match(css, /--tip-rise: calc\(var\(--motion-rise\) \* var\(--tip-dir, 1\)\);/);
    assert.match(css, /\[data-tip\]::after \{[^}]*text-align: start;/, "a right-to-left tip lines up with its own start");
    assert.match(css, /@media \(prefers-reduced-motion: reduce\) \{[^}]*\[data-tip\] \{\s*--tip-rise: 0px;/, "a flipped tip still only fades when reduced");
  });

  /* A tip is painted inside its element, so on the first row of the scrolling session list its
     four lines were cut off by the list. The room it needs comes from its text, not one line. */
  it("flips a multi-line tip by the room its text needs", () => {
    const doc = fakeDocument();
    const list = { parentElement: null, getBoundingClientRect: () => ({ left: 0, right: 320, top: 118, bottom: 900 }) };
    const rows = { parentElement: list, getBoundingClientRect: () => ({ left: 0, right: 320, top: 0, bottom: 4000 }) };
    const styles = new Map([[list, { overflowX: "auto", overflowY: "auto" }], [rows, visible]]);
    keepTipsInView(doc, { innerWidth: 1440, innerHeight: 900, getComputedStyle: (node) => styles.get(node) });
    const row = (top, tip, below = false) => {
      const el = element();
      el.setAttribute("data-tip", tip);
      if (below) el.setAttribute("data-tip-below", "");
      return Object.assign(el, {
        toggleAttribute: (name, force) => (force ? el.setAttribute(name, "") : el.removeAttribute(name)),
        parentElement: rows,
        getBoundingClientRect: () => ({ left: 0, right: 300, top, bottom: top + 28 }),
        style: { setProperty: () => {} },
        closest: () => el
      });
    };
    const fourLines = "A title\nAgent · Open\nStarted Oct 20\nwf-1";
    assert.equal(tipHeight(fourLines), 4 * 15.4 + 22);
    assert.equal(tipHeight("x".repeat(2_000)), 8 * 15.4 + 22, "eight lines at most, as app.css clips it");

    const top = row(118 + 60, fourLines);
    doc.handlers.pointerover({ target: top });
    assert.ok(top.hasAttribute("data-tip-flip"), "60px under the list's top is room for one line, not four");
    const oneLine = row(118 + 60, "A title");
    doc.handlers.pointerover({ target: oneLine });
    assert.ok(!oneLine.hasAttribute("data-tip-flip"), "the same spot holds a one-line tip");

    const roomy = row(118 + 200, fourLines);
    doc.handlers.focusin({ target: roomy });
    assert.ok(!roomy.hasAttribute("data-tip-flip"));

    /* Scrolled back down, the same row shows its tip above again. */
    top.getBoundingClientRect = () => ({ left: 0, right: 300, top: 600, bottom: 628 });
    doc.handlers.pointerover({ target: top });
    assert.ok(!top.hasAttribute("data-tip-flip"));

    const bottom = row(900 - 28, fourLines);
    doc.handlers.pointerover({ target: bottom });
    assert.ok(!bottom.hasAttribute("data-tip-flip"), "the last row keeps its tip above");

    const below = row(118, fourLines, true);
    doc.handlers.pointerover({ target: below });
    assert.ok(!below.hasAttribute("data-tip-flip"), "a below tip with room below stays below");
  });

  /* A hidden tip was only transparent, so it was still laid out: the bottom drawer's centered
     tip, 12px from the window's edge, made the page 1531px wide at 1440. */
  it("takes no room while hidden, so no tip can widen the page", () => {
    const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    const rest = /\n\[data-tip\]::before,\n\[data-tip\]::after,\n\[data-tip\] > \.tip-card \{([^}]*)\}/.exec(css)?.[1] ?? "";
    assert.match(rest, /display: none;/);
    assert.doesNotMatch(rest, /opacity/);
    assert.match(
      css,
      /\[data-tip\]:hover::before,\s*\[data-tip\]:hover::after,\s*\[data-tip\]:focus-visible::before,\s*\[data-tip\]:focus-visible::after,\s*\[data-tip\]:hover > \.tip-card,\s*\[data-tip\]:focus-visible > \.tip-card \{\s*display: block;\s*animation: tip-in/
    );
    assert.match(css, /\[data-tip\]:focus-visible::after,\s*\[data-tip\]:focus-visible > \.tip-card \{\s*animation-delay: 0s;/, "focus asked for it, so no wait");
    assert.match(css, /\[data-tip\]:has\(> \.tip-card\)::after \{\s*content: none;/, "a card replaces the text bubble, not stacks on it");
  });

  /* A rich tip is a real element, so its own height decides the flip, not a guess from text;
     and its element can fill it in as it shows, so it is measured again a frame later. */
  it("flips a rich tip by its card's measured height, measured again once it has filled in", () => {
    const doc = fakeDocument();
    const frames = [];
    const list = { parentElement: null, getBoundingClientRect: () => ({ left: 0, right: 320, top: 100, bottom: 900 }) };
    keepTipsInView(doc, { innerWidth: 1440, innerHeight: 900, getComputedStyle: () => ({ overflowX: "auto", overflowY: "auto" }), requestAnimationFrame: (f) => frames.push(f) });
    const card = { offsetHeight: 60, textContent: "" };
    const on = new Set();
    const row = {
      parentElement: list,
      getAttribute: () => "",
      hasAttribute: (name) => on.has(name),
      toggleAttribute: (name, force) => (force ? on.add(name) : on.delete(name)),
      getBoundingClientRect: () => ({ left: 0, right: 300, top: 200, bottom: 228 }),
      style: { setProperty: () => {} },
      querySelector: (selector) => (selector === ":scope > .tip-card" ? card : null)
    };
    doc.handlers.pointerover({ target: { closest: () => row } });
    assert.ok(!on.has("data-tip-flip"), "100px above holds a 60px card");
    card.offsetHeight = 140;
    frames.shift()();
    assert.ok(on.has("data-tip-flip"), "the card it grew into does not fit, so it flips");
  });

  /* A Session Manager row's tip stood on top of the rows above it. Beside the row it covers
     nothing, but the list clips, so a painted bubble there would be cut off at the drawer's edge. */
  it("puts a side tip beside its row, fixed to the window so the list cannot clip it", () => {
    const doc = fakeDocument();
    const frames = [];
    const list = { parentElement: null, getBoundingClientRect: () => ({ left: 0, right: 420, top: 100, bottom: 900 }) };
    keepTipsInView(doc, { innerWidth: 1440, innerHeight: 900, getComputedStyle: () => ({ overflowX: "auto", overflowY: "auto" }), requestAnimationFrame: (f) => frames.push(f) });
    const side = (rect, height = 60) => {
      const vars = new Map();
      const on = new Set();
      const row = {
        parentElement: list,
        getAttribute: (name) => (name === "data-tip-side" ? "right" : ""),
        hasAttribute: (name) => on.has(name),
        toggleAttribute: (name, force) => (force ? on.add(name) : on.delete(name)),
        getBoundingClientRect: () => rect,
        style: { setProperty: (name, value) => vars.set(name, value) },
        querySelector: () => ({ offsetHeight: height, textContent: "" })
      };
      doc.handlers.focusin({ target: { closest: () => row } });
      return { vars, flipped: on.has("data-tip-flip") };
    };

    const row = side({ left: 0, right: 420, top: 300, bottom: 328 });
    assert.ok(!row.flipped);
    assert.equal(row.vars.get("--tip-side-x"), "420px", "from the row's right edge, past the list that clips it");
    assert.equal(row.vars.get("--tip-side-middle"), "314px");
    assert.equal(row.vars.get("--tip-side-top"), `${314 - 30}px`, "centred on the row");
    assert.equal(row.vars.get("--tip-side-room"), `${1440 - TIP_MARGIN - 12 - 420}px`);
    assert.equal(side({ left: 0, right: 420, top: 880, bottom: 900 }).vars.get("--tip-side-top"), `${900 - TIP_MARGIN - 60}px`, "held inside the window");
    const crowded = side({ left: 600, right: 1380, top: 300, bottom: 328 });
    assert.ok(crowded.flipped, "no room on the right: it goes left");
    assert.equal(crowded.vars.get("--tip-side-x"), "600px");

    const controls = readFileSync(new URL("../chat/SessionControls.svelte", import.meta.url), "utf8");
    assert.match(controls, /class=\{\["session-row"[\s\S]*?data-tip=""\s*data-tip-side="right"/, "a session row asks for its tip on the right");
    const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    assert.match(css, /\[data-tip\]\[data-tip-side="right"\] > \.tip-card \{\s*position: fixed;/, "fixed, so no overflow above it clips it");
    assert.match(css, /\[data-tip\]\[data-tip-side="right"\]\[data-tip-flip\] > \.tip-card \{\s*top: var\(--tip-side-top\);\s*left: calc\(var\(--tip-side-x\) \+ 12px\);/);
    assert.match(css, /\[data-tip\]\[data-tip-side="right"\]\[data-tip-flip\] > \.tip-card \{\s*left: calc\(var\(--tip-side-x\) - 12px\);\s*transform: translate\(-100%, 0\);/);
  });
});
