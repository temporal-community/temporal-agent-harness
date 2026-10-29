// ABOUTME: Pins the token figure beside every model name, including the single-row case used by
// a short run whose only metered call is Jev's approval evaluation.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { buildUsageTimeline, summarizeCost } from "$lib/cost/pricing.ts";
import ModelBreakdown from "./ModelBreakdown.svelte";

/* A summary that is already the run's end, with the run's points made from it: one call
   per row, in row order. */
const pointsOf = (summary) =>
  summary.modelBreakdown.map((row, index) => ({
    index: index + 1,
    timestamp: index,
    model: row.model,
    call: row.unreportedCalls > 0 && row.tokens.total === 0 ? null : row.tokens,
    tokens: row.tokens,
    estimatedCostUsd: null
  }));
const atEnd = ({ props }) => render(ModelBreakdown, { props: { ...props, points: pointsOf(props.usage) } });

const usage = {
  tokens: { input: 1200, output: 345, thought: 0, cached: 0, toolUse: 0, total: 1545 },
  estimatedCostUsd: null,
  modelBreakdown: [
    {
      model: "jev-review-model",
      tokens: { input: 1200, output: 345, thought: 0, cached: 0, toolUse: 0, total: 1545 },
      estimatedCostUsd: null,
      unreportedCalls: 0
    }
  ],
  unreportedCalls: 0
};

const visibleText = (html) =>
  html
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();

describe("ModelBreakdown", () => {
  it("shows the token count when Jev is the only model", () => {
    const text = visibleText(atEnd({ props: { usage } }).body);

    assert.match(text, /jev-review-model/);
    assert.match(text, /1,545/, "Tokens by model must pair the model name with its token count");
  });

  it("draws one line per model: a truncating name, a share bar and a figure, largest first", () => {
    const long = `claude-sonnet-${"long-model-id-".repeat(12)}2026`;
    const totals = (total) => ({ input: total, output: 0, thought: 0, cached: 0, toolUse: 0, total });
    const html = atEnd({
      props: {
        usage: {
          tokens: totals(4000),
          estimatedCostUsd: null,
          unreportedCalls: 1,
          modelBreakdown: [
            { model: "small", tokens: totals(1000), estimatedCostUsd: null, unreportedCalls: 0 },
            { model: long, tokens: totals(3000), estimatedCostUsd: null, unreportedCalls: 0 },
            { model: "نموذج-王", tokens: totals(0), estimatedCostUsd: null, unreportedCalls: 1 }
          ]
        }
      }
    }).body;
    const rows = [...html.matchAll(/<li[^>]*>([\s\S]*?)<\/li>/g)].map((m) => m[1]);
    assert.equal(rows.length, 3);
    const names = rows.map((row) => row.match(/class="name[^"]*"[^>]*><span[^>]*>([^<]*)</)[1]);
    assert.deepEqual(names, [long, "small", "نموذج-王"], "sorted by tokens, descending");
    assert.match(rows[0], new RegExp(`dir="auto"[^>]*data-tip="${long}"`), "the full id is in the tip, and the text runs in its own direction");
    assert.match(rows[0], /inline-size: 100%/, "the largest spender fills its track");
    assert.match(rows[1], /inline-size: 33\.3/, "and the others are drawn against it; the share is the percentage");
    assert.match(visibleText(rows[0]), /3,000 75%/);
    const source = readFileSync(new URL("./ModelBreakdown.svelte", import.meta.url), "utf8");
    const nameRule = source.match(/\n {2}\.name \{([^}]*)\}/)[1];
    assert.doesNotMatch(nameRule, /overflow: hidden/, "the tip's own element must not clip the tip it draws");
    assert.match(visibleText(rows[2]), /—/, "a model that reported nothing has no figure, not a zero");
  });

  it("draws the whitespace hiding in a model id, on one line", () => {
    const totals = { input: 10, output: 0, thought: 0, cached: 0, toolUse: 0, total: 10 };
    const row = (model) => ({ model, tokens: totals, estimatedCostUsd: null, unreportedCalls: 0 });
    const html = atEnd({
      props: { usage: { tokens: totals, estimatedCostUsd: null, unreportedCalls: 0, modelBreakdown: [row("gpt\n5\tmini"), row(" gpt-5 "), row("")] } }
    }).body;
    const names = [...html.matchAll(/class="name[^"]*"[^>]*><span[^>]*>([^<]*)</g)].map((m) => m[1]);
    assert.deepEqual(names, ["gpt↵5→mini", " gpt-5 ", "(empty model id)"]);
    const source = readFileSync(new URL("./ModelBreakdown.svelte", import.meta.url), "utf8");
    assert.match(source, /\.name > span \{[^}]*white-space: pre;/, "edge spaces stay visible and the id stays on one line");
  });

  /* Scrubbing used to reorder the rows and add new ones, and each new row resized the
     chart above it. The run's end fixes the order and the bars' scale. */
  it("keeps every model's row, in run-final order, while scrubbing", () => {
    let clock = 0;
    const ended = (model, total) => ({
      event: "model_interaction_ended",
      data: { type: "model_interaction_ended", timestamp: (clock += 1), model, usage: { input_tokens: total, output_tokens: 0, total_tokens: total } }
    });
    const frames = [ended("early", 300), ended("late", 100), ended("late", 900)];
    const points = buildUsageTimeline(frames);
    const at = (cursor) => render(ModelBreakdown, { props: { usage: summarizeCost(frames.slice(0, cursor)), points } }).body;
    const order = (html) => [...html.matchAll(/class="name[^"]*"[^>]*><span[^>]*>([^<]*)</g)].map((m) => m[1]);
    const widths = (html) => [...html.matchAll(/inline-size: ([\d.]+)%/g)].map((m) => Number(m[1]));

    for (const cursor of [0, 1, 2, 3]) assert.deepEqual(order(at(cursor)), ["late", "early"], `cursor ${cursor}`);
    const first = at(1);
    assert.match(first, /<li class="pending[^"]*"[\s\S]*?>late</, "a model not called yet is listed, dimmed");
    assert.deepEqual(widths(first), [0, 30], "bars scale against the largest final total (1,000)");
    assert.deepEqual(widths(at(3)), [100, 30], "so they only grow, and the biggest spender ends full");
    const ch = (html) => html.match(/--value-ch: (\d+)ch/)[1];
    assert.equal(ch(at(0)), ch(at(3)), "the figure column is sized for the run's widest figure from the start");
  });

  it("lists recent turns at the cursor without another chart", () => {
    const points = Array.from({ length: 7 }, (_, index) => ({
      index: index + 1,
      timestamp: index,
      turnNumber: index + 1,
      model: "model",
      call: { input: 100, output: index, thought: 0, cached: 0, toolUse: 0, total: 100 + index },
      tokens: { input: 100, output: index, thought: 0, cached: 0, toolUse: 0, total: 100 + index },
      estimatedCostUsd: null,
      durationSeconds: null
    }));
    const html = render(ModelBreakdown, { props: { usage, points, viewIndex: 7 } }).body;
    const text = visibleText(html);
    assert.match(text, /By turn · latest 5/);
    assert.doesNotMatch(text, /Turn 2\b/);
    assert.match(text, /Turn 3 102/);
    assert.match(text, /Turn 7 106/);
    const source = readFileSync(new URL("./ModelBreakdown.svelte", import.meta.url), "utf8");
    assert.match(source, /\.turn-list \{[^}]*grid-template-columns: repeat\(5, minmax\(0, 1fr\)\)/, "five turns stay on one row");
    assert.match(source, /\.turn-list \{[^}]*direction: ltr;/, "RTL keeps the first and last turn inside the same fixed row");
  });
});
