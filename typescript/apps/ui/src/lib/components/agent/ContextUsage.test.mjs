// ABOUTME: Asserts the context indicator reads the session agent's latest model call against the
// window the producer reported, and that with no reported window there is no reading at all —
// never a percent of a guessed limit. This is the check that fails when someone hardcodes a
// model's window, or counts a subagent's call against the session's context.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { contextWindowUsage } from "$lib/cost/pricing.ts";
import ContextUsage from "./ContextUsage.svelte";

const ended = (role, usage, context_window) => ({
  role,
  frame: {
    event: "model_interaction_ended",
    data: { type: "model_interaction_ended", timestamp: 1, model: "m", usage, context_window }
  }
});

const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();

describe("contextWindowUsage", () => {
  it("reads the root's latest call: prompt plus reply, over its reported window", () => {
    const usage = contextWindowUsage([
      ended("parent", { input_tokens: 10, output_tokens: 5 }, 1000),
      ended("parent", { input_tokens: 32_000, output_tokens: 768 }, 128_000),
      ended("subagent", { input_tokens: 90_000, output_tokens: 1 }, 100_000)
    ]);
    assert.deepEqual(usage, { used: 32_768, limit: 128_000 });
  });

  it("is null when the producer reports no window, rather than a percent of a guess", () => {
    assert.equal(contextWindowUsage([ended("parent", { input_tokens: 32_000, output_tokens: 768 })]), null);
    assert.equal(contextWindowUsage([ended("parent", { input_tokens: 1, output_tokens: 1 }, 0)]), null);
    assert.equal(contextWindowUsage([ended("parent", { input_tokens: 1 }, "128k")]), null);
    assert.equal(contextWindowUsage([ended("parent", { input_tokens: null, output_tokens: 9 }, 1000)]), null);
    assert.equal(contextWindowUsage([]), null);
  });
});

describe("ContextUsage", () => {
  it("shows the percent on the chip and the counts over a bar in the card", () => {
    const html = render(ContextUsage, { props: { usage: { used: 32_768, limit: 128_000 } } }).body;
    assert.match(text(html), /25\.6% context/);
    assert.match(text(html), /Context 32,768 \/ 128,000/);
    assert.match(html, /<progress[^>]*value="32768"[^>]*max="128000"[^>]*aria-label="Context used"/);
  });

  it("never reads a filled window as 0%", () => {
    const html = render(ContextUsage, { props: { usage: { used: 11_400, limit: 2_000_000_000 } } }).body;
    assert.match(text(html), /&lt;0\.1% context/);
  });
});
