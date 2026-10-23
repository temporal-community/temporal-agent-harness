import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, describe, it, vi } from "vitest";

import { dissolveClear, inputClearDissolve } from "./inputClearDissolve.ts";

function surface({ reduced = false } = {}) {
  const children = [];
  const classes = new Set();
  const wrapper = {
    children,
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name)
    },
    append(ghost) {
      children.push(ghost);
      ghost.remove = () => children.splice(children.indexOf(ghost), 1);
    }
  };
  const node = {
    value: "search words",
    parentElement: wrapper,
    offsetLeft: 24,
    offsetTop: 0,
    clientWidth: 180,
    clientHeight: 28
  };
  vi.stubGlobal("window", { setTimeout, clearTimeout });
  vi.stubGlobal("matchMedia", () => ({ matches: reduced }));
  vi.stubGlobal("getComputedStyle", () => ({
    getPropertyValue: (name) => (name === "--duration-pane" ? "220ms" : ""),
    font: "11px sans-serif",
    letterSpacing: "normal",
    lineHeight: "normal",
    textAlign: "start",
    direction: "ltr",
    padding: "0px"
  }));
  vi.stubGlobal("document", {
    createElement: () => ({
      style: {},
      addEventListener() {},
      remove() {}
    })
  });
  return { node, wrapper };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("input clear dissolve", () => {
  it("clears immediately, keeps one outgoing copy, then removes it after the motion token", () => {
    vi.useFakeTimers();
    const { node, wrapper } = surface();
    inputClearDissolve(node);

    dissolveClear(node, () => (node.value = ""));

    assert.equal(node.value, "");
    assert.equal(wrapper.children.length, 1);
    assert.equal(wrapper.children[0].textContent, "search words");
    vi.advanceTimersByTime(219);
    assert.equal(wrapper.children.length, 1);
    vi.advanceTimersByTime(1);
    assert.equal(wrapper.children.length, 0);
  });

  it("clears without rendering an outgoing copy under reduced motion", () => {
    const { node, wrapper } = surface({ reduced: true });
    inputClearDissolve(node);

    dissolveClear(node, () => (node.value = ""));

    assert.equal(node.value, "");
    assert.equal(wrapper.children.length, 0);
  });

  it("shows one shared clear control only for text and returns focus after clearing", () => {
    for (const relative of [
      "../components/agent/TranscriptPanel.svelte",
      "../components/chat/SessionControls.svelte",
      "../components/chat/StartSessionDialog.svelte"
    ]) {
      const source = readFileSync(new URL(relative, import.meta.url), "utf8");
      assert.match(source, /\{#if (?:query|sessionSearch)\}[\s\S]*?<IconButton class="clear-search" label="Clear search"/);
      assert.match(source, /dissolveClear\([^,]+, \(\) => \((?:query|sessionSearch) = ""\)\);/);
      assert.match(source, /\.focus\(\{ preventScroll: true \}\);/);
    }
  });

  it("clips its field only while a ghost is out, so tips and cards inside it can show", () => {
    const css = readFileSync(new URL("../../app.css", import.meta.url), "utf8");
    const rest = (css.match(/^\.clearable \{([^}]*)\}/m)?.[1] ?? "").split("\n").filter((line) => /^\s*[a-z-]+\s*:/.test(line)).join("\n");
    assert.doesNotMatch(rest, /overflow\s*:/, "the composer form wears this class, and a clip there hides every popover in it");
    assert.match(rest, /min-width: 0;/);
    assert.match(css, /^\.clearable\.has-leaving-text \{\s*overflow: hidden;\s*\}/m);
  });

  it("fades, shrinks and drifts the copy down on the pane clock, inside a field that still wears the class", () => {
    const css = readFileSync(new URL("../../app.css", import.meta.url), "utf8");
    assert.match(css, /^\.clearable > \.leaving-text \{\s*animation: text-leaves var\(--duration-pane\) var\(--ease-out\) forwards;\s*\}/m);
    assert.match(css, /@keyframes text-leaves \{\s*to \{\s*opacity: 0;\s*transform: translateY\(var\(--motion-rise\)\) scale\(var\(--motion-scale\)\);/);
    assert.doesNotMatch(css.match(/^\.leaving-text \{([^}]*)\}/m)?.[1] ?? "", /animation/, "a field that lost the class drops its copy unfaded");
  });
});
