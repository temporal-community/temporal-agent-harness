// ABOUTME: The agent mark is decoration unless a caller names it, and is then an image with
// that name, never both hidden and named.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import IconAgent from "./IconAgent.svelte";

const html = (props = {}) => render(IconAgent, { props }).body;

describe("IconAgent", () => {
  it("is hidden from assistive tech by default", () => {
    const svg = html();
    assert.match(svg, /<svg[^>]*aria-hidden="true"/);
    assert.match(svg, /width="15"/);
    assert.doesNotMatch(svg, /role=|aria-label=/);
  });

  it("becomes a named image when given a label", () => {
    const svg = html({ label: "Agent", size: 20 });
    assert.match(svg, /role="img"/);
    assert.match(svg, /aria-label="Agent"/);
    assert.match(svg, /width="20"/);
    assert.doesNotMatch(svg, /aria-hidden/);
  });
});
