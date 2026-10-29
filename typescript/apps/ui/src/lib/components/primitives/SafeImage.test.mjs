// ABOUTME: Pins SafeImage's server markup: an allowed image is a lazy, async-decoded <img> with its
// alt (or "Generated image") inside a button that opens a native <dialog>, whose full-size copy is
// not drawn until it opens. A too-large inline image is a notice, and a source the policy refuses
// is only its alt text, with no <img> at all.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import SafeImage from "./SafeImage.svelte";

const PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
const markup = (props) => stripComments(render(SafeImage, { props }).body);

describe("SafeImage", () => {
  it("draws an allowed image lazily, with its alt, behind a dialog trigger", () => {
    const body = markup({ src: PNG, alt: "A red square" });
    const images = body.match(/<img\b[^>]*>/g) ?? [];
    assert.equal(images.length, 1, "the full-size copy waits for the dialog to open");
    assert.match(images[0], /loading="lazy"/);
    assert.match(images[0], /decoding="async"/);
    assert.match(images[0], /alt="A red square"/);
    assert.match(body, /<button[^>]*aria-haspopup="dialog"[^>]*aria-label="Open full size: A red square"/);
    assert.match(body, /<dialog[^>]*aria-label="A red square"/);
    assert.doesNotMatch(body, /<dialog[^>]*\sopen/);
    assert.match(body, /aria-label="Close image"/);
  });

  it("names an image with no alt", () => {
    assert.match(markup({ src: PNG }), /<img[^>]*alt="Generated image"/);
  });

  it("serves a blob relative to the page, so a path prefix applies", () => {
    const key = "f".repeat(64);
    assert.match(markup({ src: `/api/blobs/${key}.bin`, alt: "x" }), new RegExp(`<img[^>]*src="api/blobs/${key}\\.bin"`));
  });

  it("says an inline image is too large instead of decoding it", () => {
    const body = markup({ src: `data:image/png;base64,${"A".repeat(6_000_000)}`, alt: "Huge" });
    assert.match(body, /Image too large to preview \(4\.3 MB\)/);
    assert.match(body, /Huge/);
    assert.doesNotMatch(body, /<img/);
  });

  it("draws a refused source as its alt text only", () => {
    for (const src of ["https://evil.example/p.png", "data:image/svg+xml;base64,PHN2Zz4="]) {
      const body = markup({ src, alt: "Chart" });
      assert.doesNotMatch(body, /<img|evil|<button|<dialog/);
      assert.match(body, /Chart/);
    }
  });
});
