// ABOUTME: While a reply streams, usage has not been reported yet (it arrives with
// model_interaction_ended), so the Model interaction card used to read "0 / 0" tokens and look like
// a free reply. It must say "streaming…" in the muted pending tone instead, and settle to the real
// totals in the ordinary tone once the interaction ends.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { buildAgentGraph } from "$lib/state/flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });
const usage = { input_tokens: 1200, output_tokens: 340, total_tokens: 1540 };

const metrics = (frames) =>
  Object.fromEntries(
    buildAgentGraph(frames)
      .nodes.find((node) => node.id.endsWith("model"))
      .data.metrics.map((metric) => [metric.label, `${metric.value} (${metric.tone ?? "neutral"})`])
  );

const streaming = [
  frame("turn_started", { user_message: "hi", turn_number: 1 }),
  frame("model_interaction_started", { model: "claude" }),
  frame("reply_delta", { text: "Hel" })
];
const ended = frame("model_interaction_ended", { model: "claude", usage });

describe("Model interaction token counts", () => {
  it("show a muted streaming placeholder instead of 0 while the first reply streams", () => {
    assert.deepEqual(metrics(streaming), {
      input: "streaming… (pending)",
      output: "streaming… (pending)"
    });
  });

  it("settle to the final totals when the interaction ends", () => {
    assert.deepEqual(metrics([...streaming, ended]), {
      input: "1,200 (neutral)",
      output: "340 (neutral)"
    });
  });

  it("keep earlier totals visible while a later reply streams", () => {
    const next = [
      frame("turn_started", { user_message: "again", turn_number: 2 }),
      frame("model_interaction_started", { model: "claude" })
    ];
    assert.deepEqual(metrics([...streaming, ended, ...next]), {
      input: "1,200 + streaming… (pending)",
      output: "340 + streaming… (pending)"
    });
  });

  it("are drawn muted by the metric strip", () => {
    const strip = readFileSync(
      new URL("../src/lib/components/primitives/MetricStrip.svelte", import.meta.url),
      "utf8"
    );
    assert.match(strip, /\.pending dd\s*\{[^}]*color:\s*var\(--text-3\)/);
  });
});
