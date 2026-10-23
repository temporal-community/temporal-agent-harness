// ABOUTME: Asserts textWidth, the label-fit measure behind the latency waterfall's bars: without a
// canvas it must return the old per-character estimate rather than throw, and with one it must
// measure each string once and answer repeats from the cache.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { textWidth } from "./textWidth.ts";

describe("textWidth", () => {
  it("falls back to the per-character estimate where there is no canvas", () => {
    assert.equal(textWidth("search · 1.2s", "10px sans-serif"), 13 * 6.6);
  });

  it("measures a string once and caches the slack-padded width", () => {
    let calls = 0;
    const ctx = { font: "", measureText: (text) => (calls++, { width: text.length * 5 }) };
    const first = textWidth("検索ツール · 3s", "10px sans-serif", ctx);
    assert.equal(textWidth("検索ツール · 3s", "10px sans-serif", ctx), first);
    assert.equal(first, 10 * 5 + 2);
    assert.equal(calls, 1);
  });
});
