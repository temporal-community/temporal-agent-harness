// ABOUTME: Pins the browser tab to the replay controller's semantic tone map and
// keeps every favicon rendering on the same Temporal mark as the packaged logo.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { describe, it } from "vitest";
import {
  faviconDataUrl,
  faviconSvg,
  faviconToken,
  setFaviconTone,
  TEMPORAL_MARK_PATH
} from "$lib/state/favicon.ts";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");
const [indexHtml, logoAsset, faviconAsset] = await Promise.all([
  read("../../../index.html"),
  read("../../../public/temporal-logo.svg"),
  read("../../../public/favicon.svg")
]);

const pathData = (text, source) => {
  const found = [...text.matchAll(/\sd="(M[^"]+)"/g)].map((match) => match[1]);
  assert.equal(found.length, 1, `${source} should hold exactly one mark path`);
  return found[0];
};

describe("the step favicon", () => {
  it("uses the existing semantic tone tokens", () => {
    assert.deepEqual(
      {
        idle: faviconToken(null),
        neutral: faviconToken("neutral"),
        agent: faviconToken("agent"),
        model: faviconToken("model"),
        tool: faviconToken("tool"),
        approval: faviconToken("approval"),
        done: faviconToken("done"),
        error: faviconToken("error"),
        queue: faviconToken("queue")
      },
      {
        idle: "--accent",
        neutral: "--text-3",
        agent: "--accent",
        model: "--model",
        tool: "--tool",
        approval: "--queue",
        done: "--success",
        error: "--error",
        queue: "--queue"
      }
    );
  });

  it("ships a relative static fallback under path-prefixed installs", () => {
    assert.match(
      indexHtml,
      /<link id="app-favicon" rel="icon" type="image\/svg\+xml" sizes="any" href="\.\/favicon\.svg" \/>/
    );
  });

  it("keeps the static and dynamic icons on the product's Temporal mark", () => {
    const logoPath = pathData(logoAsset, "temporal-logo.svg");
    assert.equal(pathData(faviconAsset, "favicon.svg"), logoPath);
    assert.equal(TEMPORAL_MARK_PATH, logoPath);
  });

  it("builds an encoded SVG with a full tone field and contrasting mark", () => {
    const svg = faviconSvg("#ff6bff", "#0b0b10");
    assert.match(svg, /<rect width="192" height="192" fill="#ff6bff"\/>/);
    assert.match(svg, /<path d="M123\.34/);
    assert.match(svg, /fill="#0b0b10"\/>/);

    const encoded = faviconDataUrl("#ff6bff", "#0b0b10");
    assert.ok(encoded.startsWith("data:image/svg+xml,"));
    assert.equal(decodeURIComponent(encoded.slice("data:image/svg+xml,".length)), svg);
  });

  it("updates the existing head link from computed tokens and avoids duplicate paints", () => {
    const originalDocument = globalThis.document;
    const originalGetComputedStyle = globalThis.getComputedStyle;
    let writes = 0;
    let href = "./favicon.svg";
    const icon = {
      dataset: {},
      get href() {
        return href;
      },
      set href(value) {
        writes += 1;
        href = value;
      }
    };
    const colors = {
      "--model": "#a9aef5",
      "--queue": "#ff6bff",
      "--surface-0": "#0b0b10"
    };

    globalThis.document = {
      documentElement: {},
      querySelector(selector) {
        assert.equal(selector, "#app-favicon");
        return icon;
      }
    };
    globalThis.getComputedStyle = () => ({
      getPropertyValue(token) {
        return colors[token] ?? "";
      }
    });

    try {
      setFaviconTone("model");
      assert.equal(writes, 1);
      assert.match(decodeURIComponent(href), /fill="#a9aef5"/);

      setFaviconTone("model");
      assert.equal(writes, 1, "an unchanged color should not repaint the browser tab");

      setFaviconTone("approval");
      assert.equal(writes, 2);
      assert.match(decodeURIComponent(href), /fill="#ff6bff"/);
    } finally {
      if (originalDocument === undefined) delete globalThis.document;
      else globalThis.document = originalDocument;
      if (originalGetComputedStyle === undefined) delete globalThis.getComputedStyle;
      else globalThis.getComputedStyle = originalGetComputedStyle;
    }
  });
});
