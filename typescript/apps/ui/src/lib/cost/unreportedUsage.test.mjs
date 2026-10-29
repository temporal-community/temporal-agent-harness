// ABOUTME: One rule for a model call that ended with no usage object, held by one helper,
// tokenFigure: a sum that includes such a call is a lower bound ("≥ N"), and with nothing reported
// it is unknown ("—"), never the zero an empty sum prints. Asserted through every surface that
// shows a token count — the Tokens pane, the graph's model node and the Logs row — so a surface
// that formats its own count again fails here.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { tokenFigure } from "./pricing.ts";
import { buildAgentGraph } from "../state/flowProjection.ts";
import { buildReplayLog } from "../state/replayLog.ts";

let clock = 1_789_126_400;
const meta = () => ({ turn_id: "turn-001", turn_number: 1, message_id: "msg-001", agent_id: "7f3c1a", timestamp: (clock += 1) });
const started = (model) => ({ event: "model_interaction_started", data: { type: "model_interaction_started", ...meta(), model } });
const ended = (model, usage) => ({ event: "model_interaction_ended", data: { type: "model_interaction_ended", ...meta(), model, usage } });
const spent = { input_tokens: 1000, output_tokens: 400, thought_tokens: 0, cached_tokens: 0, tool_use_tokens: 0, total_tokens: 1400 };

describe("tokenFigure", () => {
  it("is exact when every call reported, a floor after a silent one, unknown when none did", () => {
    assert.equal(tokenFigure(1400, 0), "1,400");
    assert.equal(tokenFigure(0, 0), "0", "a real zero is still a zero");
    assert.equal(tokenFigure(1400, 1), "≥ 1,400");
    assert.equal(tokenFigure(0, 2), "—", "nothing reported is not a measured zero");
  });
});

describe("the graph's model node", () => {
  const metrics = (frames) => buildAgentGraph(frames).nodes.find((node) => node.id === "model")?.data.metrics ?? [];

  it("reads a call with no usage as unknown, not zero", () => {
    const shown = metrics([started("relay"), ended("relay", null)]);
    assert.deepEqual(shown.map((metric) => metric.value), ["—", "—"]);
    assert.ok(shown.every((metric) => metric.note), "and says why in a tip");
  });

  it("marks a sum after a silent call as a lower bound", () => {
    const shown = metrics([started("m"), ended("m", spent), started("relay"), ended("relay", null)]);
    assert.deepEqual(shown.map((metric) => metric.value), ["≥ 1,000", "≥ 400"]);
  });

  it("keeps exact figures when every call reported", () => {
    assert.deepEqual(metrics([started("m"), ended("m", spent)]).map((metric) => metric.value), ["1,000", "400"]);
  });
});

describe("the Logs row for a finished call", () => {
  const row = (frame) => buildReplayLog([frame]).rows.find((item) => item.label === "Model completed");

  it("says usage was not reported, and carries no zero into the turn's total", () => {
    const silent = row(ended("relay", null));
    assert.equal(silent.body, "Usage not reported");
    assert.equal(silent.usage, undefined, "a zero here would be summed into the turn's tokens");
  });

  it("keeps the count of a call that reported", () => {
    const measured = row(ended("m", spent));
    assert.equal(measured.body, "1,400 tokens");
    assert.equal(measured.usage.total, 1400);
  });
});

it("every token surface writes counts through tokenFigure", () => {
  for (const path of [
    "../components/flow/UsageReading.svelte",
    "../components/flow/ModelBreakdown.svelte",
    "../state/flowProjection.ts",
    "../state/replayLog.ts"
  ]) {
    assert.match(readFileSync(new URL(path, import.meta.url), "utf8"), /\btokenFigure\(/, path);
  }
});
