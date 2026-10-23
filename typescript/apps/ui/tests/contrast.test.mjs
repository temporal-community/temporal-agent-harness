// ABOUTME: Pins the contrast of the faintest text token and of the focus ring against the
// surfaces they sit on. --text-4 measured 2.8:1 and the ring, drawn at 55% of the accent,
// 2.2:1: both under WCAG AA, and nothing on screen says when a token slips back.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

const css = readFileSync(new URL("../src/app.css", import.meta.url), "utf8");
const token = (name) => css.match(new RegExp(`^\\s*--${name}:\\s*([^;]+);`, "m"))?.[1].trim();

const luminance = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const contrast = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
const mix = (a, b, share) =>
  `#${[1, 3, 5]
    .map((i) => Math.round(parseInt(a.slice(i, i + 2), 16) * share + parseInt(b.slice(i, i + 2), 16) * (1 - share)))
    .map((c) => c.toString(16).padStart(2, "0"))
    .join("")}`;
const surfaces = ["surface-0", "surface-1"].map(token);

describe("token contrast", () => {
  it("keeps --text-4 at WCAG AA for text on every surface", () => {
    for (const surface of surfaces) assert.ok(contrast(token("text-4"), surface) >= 4.5, `--text-4 on ${surface}`);
  });

  /* A finished turn's header in the chat recedes to --text-3 on the chat's --surface-0. */
  it("keeps a finished turn header's quiet tone at WCAG AA on the chat", () => {
    assert.ok(contrast(token("text-3"), token("surface-0")) >= 4.5);
  });

  /* A tip's keys are muted, but --text-3 is 3.4:1 on --surface-3, so they are --text-2. */
  it("keeps a tip's keys and values at WCAG AA on the tip's surface", () => {
    for (const text of ["text-1", "text-2"]) assert.ok(contrast(token(text), token("surface-3")) >= 4.5, `--${text} on --surface-3`);
    assert.ok(contrast(token("text-3"), token("surface-3")) < 4.5, "why the keys are not --text-3");
    const card = readFileSync(new URL("../src/lib/components/primitives/TipCard.svelte", import.meta.url), "utf8");
    assert.match(card, /\.tip-card-key \{\s*color: var\(--text-2\);/);
  });

  /* A file the commit added, changed or removed draws its name in these. */
  it("keeps the status tokens at WCAG AA as text", () => {
    for (const status of ["success", "warning", "error"]) {
      for (const surface of surfaces) assert.ok(contrast(token(status), surface) >= 4.5, `--${status} on ${surface}`);
    }
  });

  /* A runtime boundary's header is tinted by its status: --text-3 measured 4.37:1 on the 12%
     green band, and the bare --accent status word 4.08:1 on its own band. */
  it("keeps a runtime boundary's quiet text and status word at WCAG AA on its tinted band", () => {
    const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
    const band = read("../src/lib/components/flow/AgentWorkflowNode.svelte").match(
      /\.workflow-head \{[^}]*background: color-mix\(in srgb, var\(--status-color\) (\d+)%, var\(--surface-1\)\)/
    );
    assert.ok(band, "the header band's tint");
    assert.match(
      read("../src/lib/components/primitives/StatusChip.svelte"),
      /\.status-text\.word \{\s*color: color-mix\(in srgb, currentColor 82%, var\(--text-1\)\);/
    );
    for (const hue of ["text-3", "accent", "reasoning", "success", "error", "live"]) {
      const background = mix(token(hue), token("surface-1"), Number(band[1]) / 100);
      assert.ok(contrast(token("text-3"), background) >= 4.5, `--text-3 on the --${hue} band`);
      assert.ok(contrast(mix(token(hue), token("text-1"), 0.82), background) >= 4.5, `the --${hue} word on its band`);
    }
  });

  /* A timeline cap draws its glyph in the state colour on a fill tinted by that colour. */
  it("keeps a timeline cap's glyph at 3:1 or more on the cap's fill", () => {
    const source = readFileSync(new URL("../src/lib/components/flow/WorkflowTimeline.svelte", import.meta.url), "utf8");
    const share = Number(
      source.match(/\n {2}\.cap \{[^}]*background: color-mix\(in srgb, currentColor (\d+)%, var\(--surface-0\)\);/)?.[1]
    );
    assert.ok(share, "the cap's fill is a tint of its state over --surface-0");
    assert.doesNotMatch(source, /\.cap[^{]*\{[^}]*\bcolor: var\(--surface/, "the glyph is the state colour, not a surface");
    const hues = [...source.matchAll(/\.kind-[a-z]+ \{ color: var\(--([a-z-]+)\); \}/g)].map(([, hue]) => hue);
    assert.ok(hues.length >= 5, "every row kind's colour");
    for (const hue of [...hues, "error"]) {
      const fill = mix(token(hue), token("surface-0"), share / 100);
      assert.ok(contrast(token(hue), fill) >= 3, `the --${hue} glyph on its ${share}% cap`);
    }
  });

  /* A token bar stacks one part per category in these, on the pane's --surface-1. */
  it("keeps every token category's bar part at 3:1 on the chart surface, and like hues apart", () => {
    const source = readFileSync(new URL("../src/lib/components/flow/usageChart.ts", import.meta.url), "utf8");
    const block = source.match(/TOKEN_CATEGORY_STYLES[^=]*= \{([\s\S]*?)\n\};/)?.[1] ?? "";
    const parts = [...block.matchAll(/(\w+): \{ label: "[^"]+", color: "([^"]+)" \}/g)].map(([, name, color]) => {
      const tinted = color.match(/^color-mix\(in srgb, var\(--([a-z0-9-]+)\) (\d+)%, var\(--surface-1\)\)$/);
      const solid = color.match(/^var\(--([a-z0-9-]+)\)$/);
      assert.ok(tinted || solid, `${name} is a token or a tint of one over the surface`);
      const hue = (tinted ?? solid)[1];
      return { name, hue, hex: tinted ? mix(token(hue), token("surface-1"), Number(tinted[2]) / 100) : token(hue) };
    });
    assert.deepEqual(parts.map((part) => part.name), ["uncached", "cached", "output", "thought", "toolUse", "decisions"]);
    for (const { name, hex } of parts) {
      for (const surface of ["surface-1", "surface-2"]) assert.ok(contrast(hex, token(surface)) >= 3, `${name} on --${surface}`);
    }
    /* Output and decisions were both mid grey and read as one key in the legend. */
    const family = (hue) => hue.replace(/-\d+$/, "");
    for (const [k, a] of parts.entries()) {
      for (const b of parts.slice(k + 1)) {
        if (family(a.hue) === family(b.hue)) assert.ok(contrast(a.hex, b.hex) >= 1.5, `${a.name} and ${b.name} differ in lightness`);
      }
    }
    assert.match(source, /stroke: "var\(--surface-1\)"/, "a surface hairline separates neighbours of a like lightness");
  });

  it("draws the focus ring in the solid accent, at 3:1 or more", () => {
    assert.equal(token("focus-ring"), "var(--accent)");
    for (const surface of surfaces) assert.ok(contrast(token("accent"), surface) >= 3, `the ring on ${surface}`);
  });
});
