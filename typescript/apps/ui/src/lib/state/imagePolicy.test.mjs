// ABOUTME: Pins which image sources the console loads: a PNG, JPEG, GIF or WebP data URL up to about
// 4 MB decoded, and exactly /api/blobs/<sha256>.bin, returned relative so a path prefix applies.
// Everything that could phone home or run script (remote, protocol-relative, SVG, javascript:,
// a near-miss blob path) is null, and an inline image over the cap is "too large", never loaded.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { MAX_INLINE_IMAGE_BYTES, formatImageBytes, imagePolicy } from "./imagePolicy.ts";

const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
const KEY = "a".repeat(64);

describe("imagePolicy", () => {
  it("loads a raster data URL as given", () => {
    for (const type of ["png", "jpeg", "gif", "webp", "PNG"]) {
      const url = `data:image/${type};base64,${PNG}`;
      assert.deepEqual(imagePolicy(url), { status: "ok", src: url });
    }
  });

  it("loads nothing that could phone home or carry script", () => {
    const rejected = [
      "https://evil.example/p.png",
      "http://evil.example/p.png",
      "//evil.example/p.png",
      "/\\evil.example/p.png",
      "javascript:alert(1)",
      `data:image/svg+xml;base64,${PNG}`,
      "data:image/svg+xml,<svg onload=alert(1)>",
      "data:image/png,rawbytes",
      "data:text/html;base64,PHNjcmlwdD4=",
      `data:image/png;base64,${PNG}"onerror="x`,
      ` data:image/png;base64,${PNG}`,
      "blob:http://127.0.0.1/1234",
      "file:///etc/passwd",
      ""
    ];
    for (const url of rejected) assert.equal(imagePolicy(url), null, url);
  });

  it("calls an inline image over about 4 MB too large, by its decoded size", () => {
    const at = "A".repeat(Math.ceil((MAX_INLINE_IMAGE_BYTES * 4) / 3));
    const over = "A".repeat(Math.ceil(((MAX_INLINE_IMAGE_BYTES + 1024) * 4) / 3));
    assert.equal(imagePolicy(`data:image/png;base64,${at}`)?.status, "ok");
    const verdict = imagePolicy(`data:image/jpeg;base64,${over}`);
    assert.equal(verdict?.status, "too-large");
    assert.equal(formatImageBytes(verdict.bytes), "4.0 MB");
  });

  it("loads exactly a content-addressed blob path, relative so a path prefix applies", () => {
    assert.deepEqual(imagePolicy(`/api/blobs/${KEY}.bin`), { status: "ok", src: `api/blobs/${KEY}.bin` });
    const nearMisses = [
      `/api/blobs/${"A".repeat(64)}.bin`,
      `/api/blobs/${"a".repeat(63)}.bin`,
      `/api/blobs/${KEY}`,
      `/api/blobs/${KEY}.bin?x=1`,
      `/api/blobs/../${KEY}.bin`,
      `api/blobs/${KEY}.bin`,
      `//api/blobs/${KEY}.bin`,
      `https://evil.example/api/blobs/${KEY}.bin`
    ];
    for (const url of nearMisses) assert.equal(imagePolicy(url), null, url);
  });
});
