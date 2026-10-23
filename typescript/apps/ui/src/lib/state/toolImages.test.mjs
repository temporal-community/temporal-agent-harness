// ABOUTME: Pins which images the console finds in a tool's output: a blob_ref with an image media
// type, an MCP-style inline base64 image part, and a data URL string, at any depth, de-duplicated.
// Only candidates imagePolicy would load (or call too large) come back; a remote URL, an SVG, a
// non-image blob, or text that is not JSON at all yields nothing.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { toolImages } from "./toolImages.ts";

const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
const KEY = "0123456789abcdef".repeat(4);
const ref = (url, media_type = "image/png") => ({ blob_ref: { key: `${KEY}.bin`, bytes: 812, media_type, url } });

describe("toolImages", () => {
  it("finds a blob_ref's same-origin URL, nested anywhere, once", () => {
    const output = JSON.stringify({ result: [ref(`/api/blobs/${KEY}.bin`)], again: ref(`/api/blobs/${KEY}.bin`) });
    assert.deepEqual(toolImages(output, "image_generation"), [{ src: `/api/blobs/${KEY}.bin`, alt: "Image from image_generation" }]);
  });

  it("finds an inline base64 image part and a data URL string", () => {
    const output = JSON.stringify({
      content: [{ type: "text", text: "Here it is" }, { type: "image", data: PNG, mimeType: "image/png" }],
      preview: `data:image/webp;base64,${PNG}`
    });
    assert.deepEqual(toolImages(output).map((image) => image.src), [`data:image/png;base64,${PNG}`, `data:image/webp;base64,${PNG}`]);
    assert.deepEqual(toolImages(`data:image/gif;base64,${PNG}`).map((image) => image.src), [`data:image/gif;base64,${PNG}`]);
  });

  it("ignores what the policy would not load, and non-image blobs", () => {
    const output = JSON.stringify({
      remote: ref("https://evil.example/p.png"),
      traversal: ref(`/api/blobs/../${KEY}.bin`),
      pdf: ref(`/api/blobs/${KEY}.bin`, "application/pdf"),
      svg: { type: "image", data: PNG, mimeType: "image/svg+xml" },
      url: "https://evil.example/p.png"
    });
    assert.deepEqual(toolImages(output), []);
    assert.deepEqual(toolImages("not json, mentions data:image/ in passing"), []);
    assert.deepEqual(toolImages(undefined), []);
  });
});
