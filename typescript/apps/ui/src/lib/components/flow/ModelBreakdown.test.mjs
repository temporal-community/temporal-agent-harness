// ABOUTME: Pins the token figure beside every model name, including the single-row case used by
// a short run whose only metered call is Jev's approval evaluation.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import ModelBreakdown from "./ModelBreakdown.svelte";

const usage = {
  tokens: { input: 1200, output: 345, thought: 0, cached: 0, toolUse: 0, total: 1545 },
  estimatedCostUsd: null,
  modelBreakdown: [
    {
      model: "jev-review-model",
      tokens: { input: 1200, output: 345, thought: 0, cached: 0, toolUse: 0, total: 1545 },
      estimatedCostUsd: null
    }
  ]
};

const visibleText = (html) =>
  html
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();

describe("ModelBreakdown", () => {
  it("shows the token count when Jev is the only model", () => {
    const text = visibleText(render(ModelBreakdown, { props: { usage } }).body);

    assert.match(text, /jev-review-model/);
    assert.match(text, /1,545/, "Tokens by model must pair the model name with its token count");
  });
});
