// ABOUTME: The graph's result box tails text only while it streams. It used to tail every
// change, so a finished, deeply nested reply opened on its last lines — a staircase of closing
// braces — instead of on what the handler said.

import assert from "node:assert/strict";
import { describe, it, vi } from "vitest";

import { resultScrollTop } from "./AgentStateNode.svelte";

/* Handle reads a live SvelteFlow canvas from context; nothing here renders, so it is stubbed. */
vi.mock("@xyflow/svelte", () => ({
  Handle: () => {},
  Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" }
}));

describe("the result box", () => {
  const box = { scrollHeight: 900 };

  it("opens a finished reply at its top", () => {
    assert.equal(resultScrollTop(box, false, true), 0);
  });

  it("follows a stream's tail while the reader is at the end", () => {
    assert.equal(resultScrollTop(box, true, true), 900);
  });

  it("leaves a reader who scrolled back mid-stream where they are", () => {
    assert.equal(resultScrollTop(box, true, false), null);
  });
});
