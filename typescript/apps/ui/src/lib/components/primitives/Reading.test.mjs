// ABOUTME: Pins the Tokens, State and Latency headers to one reading: a label, a lead figure in the
// accent, then plain figures, all through Reading. Their stats are static, and in this console a
// hairline box means "press me", so State's three bordered cards and Latency's rollup cards read as
// buttons beside a Tokens header that drew the same kind of numbers bare.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { buildUsageTimeline, summarizeCost } from "$lib/cost/pricing.ts";
import { textWidth } from "$lib/state/textWidth.ts";
import { buildStepTimeline } from "$lib/state/stepTimeline.ts";
import AgentStatePanel from "../agent/AgentStatePanel.svelte";
import LatencyWaterfall from "../flow/LatencyWaterfall.svelte";
import UsageReading from "../flow/UsageReading.svelte";

const frame = (event, data, timestamp) => ({
  event,
  data: { type: event, agent_id: "root", turn_number: 1, timestamp, ...data }
});
const usage = [frame("model_interaction_ended", { model: "m", usage: { input_tokens: 10, output_tokens: 4, total_tokens: 14 } }, 1)];
const latency = [
  frame("turn_started", { user_message: "go" }, 0),
  frame("model_interaction_started", { model: "m" }, 0),
  frame("model_interaction_ended", {}, 9),
  frame("tool_start", { tool_id: "t", tool_name: "read" }, 9),
  frame("tool_end", { tool_id: "t" }, 12)
];
const state = {
  key: "wf/plan", stateId: "plan", workflowId: "wf", label: "planner", role: "parent",
  version: 5, commits: 5, turnNumber: 1, problem: null, value: { a: 1 },
  changed: [{ op: "add", path: "/a", value: 1 }]
};

const headers = {
  tokens: render(UsageReading, { props: { usage: summarizeCost(usage), usageTimeline: buildUsageTimeline(usage), viewIndex: 0 } }).body,
  state: render(AgentStatePanel, { props: { states: [state] } }).body,
  latency: render(LatencyWaterfall, { props: { timeline: buildStepTimeline(latency), viewIndex: 0, onScrub() {} } }).body
};

describe("pane header readings", () => {
  it("draw every header's figures bare through the shared Reading, led by one accent figure", () => {
    for (const [pane, html] of Object.entries(headers)) {
      const reading = html.slice(html.indexOf('<div class="reading'));
      assert.match(html, /<div class="reading[\s"]/, `${pane}: header is not the shared Reading`);
      assert.match(reading, /<p class="kicker reading-label[^"]*" title="[^"]+"/, `${pane}: label lost its tip`);
      const strips = [...reading.matchAll(/<dl class="metric-strip([^"]*)"/g)].map((m) => m[1]);
      /* Tokens names its parts, so they stay a second strip. State and Latency have no
         such label: one strip, or the lead figure sits on a row by itself. */
      if (pane === "tokens") assert.ok(strips.length >= 2, `${pane}: a lead figure and a named breakdown`);
      else assert.equal(strips.length, 1, `${pane}: the lead figure is a strip of its own above the others`);
      for (const classes of strips) assert.match(classes, /\bbare\b/, `${pane}: a stat drawn in a hairline box`);
      assert.match(reading, /class="metric strong[\s"]/, `${pane}: the lead figure is not in the accent`);
      assert.doesNotMatch(html, /class="roll[\s"-]/, `${pane}: the old rollup cards are back`);
    }
    assert.match(headers.state, /<dd[^>]*>v5<\/dd>/, "State leads with the version at the cursor");
    assert.match(headers.latency, /class="metric strong[^"]*"[^>]*>(?:(?!<\/div>)[\s\S])*<dt class="kicker[^"]*">total</, "Latency leads with the total");
  });

  it("does not repeat the agent name, and keeps the figures on one row at the reported width", () => {
    const board = {
      ...state,
      key: "wf/board",
      stateId: "board",
      label: "Tic Tac Toe Agent",
      version: 4,
      commits: 4,
      changed: [0, 1, 4, 8].map((index) => ({ op: "replace", path: `/cells/${index}`, value: "0" }))
    };
    const html = render(AgentStatePanel, { props: { states: [board] } }).body;
    const reading = html.slice(html.indexOf('<div class="reading'), html.indexOf('class="changes"'));

    assert.match(reading, /title="board · at cursor"[^>]*>board · at cursor</);
    assert.doesNotMatch(html, /Tic Tac Toe Agent/);
    assert.match(headers.tokens, /title="At cursor"/, "the token label is unchanged");
    assert.match(headers.latency, /title="per step · 1 turn"/, "the latency label is unchanged");

    const strips = [...reading.matchAll(/<dl class="metric-strip/g)];
    assert.equal(strips.length, 1, "the lead figure is its own block above the others");
    const dl = reading.slice(reading.indexOf("<dl"), reading.indexOf("</dl>"));
    for (const label of ["version", "commits", "this step"]) {
      assert.match(dl, new RegExp(`<dt class="kicker[^"]*">${label}</dt>`));
    }
    const strong = dl.indexOf("metric strong");
    const commits = dl.indexOf(">commits<");
    assert.ok(strong !== -1 && strong < commits, "the lead figure is not in the same strip as commits");

    /* Upper bound, in px, for the three figures in the report. They fit the 303px
       reading that was stacking them, so a single strip is one row there. */
    const reportedWidth = 303;
    const cells = [
      ["v4", "VERSION"],
      ["4", "COMMITS"],
      ["4 ops", "THIS STEP"]
    ];
    const row =
      cells.reduce(
        (sum, [value, caption]) =>
          sum + Math.max(textWidth(value, "13px ui-monospace"), textWidth(caption, "500 10px ui-monospace")),
        0
      ) +
      12 * (cells.length - 1);
    assert.ok(row < reportedWidth, `figures need ${row}px, wider than the reported ${reportedWidth}px reading`);
  });

  it("gives Latency one-word figures, with count and share in the tip and a 0% kind drawn quiet", () => {
    const long = [
      frame("turn_started", { user_message: "go" }, 0),
      frame("model_interaction_started", { model: "m" }, 0),
      frame("model_interaction_ended", {}, 30),
      frame("tool_approval_requested", { tool_id: "t", tool_name: "refund" }, 30),
      frame("auto_approval_evaluation_started", { tool_id: "t", tool_name: "refund", evaluation_id: "e" }, 30),
      frame("auto_approval_evaluation_ended", { tool_id: "t", tool_name: "refund", evaluation_id: "e", verdict: "escalate" }, 31),
      frame("tool_approval_resolved", { tool_id: "t", approved: true }, 446_430),
      frame("tool_start", { tool_id: "t", tool_name: "refund" }, 446_430),
      frame("tool_end", { tool_id: "t" }, 446_452)
    ];
    const html = render(LatencyWaterfall, { props: { timeline: buildStepTimeline(long), viewIndex: 0, onScrub() {} } }).body;
    const reading = html.slice(html.indexOf('<div class="reading'), html.indexOf("</header>"));

    assert.match(reading, /title="per step · 1 turn"/);
    assert.equal([...reading.matchAll(/<dl class="metric-strip/g)].length, 1, "lead and peers share one strip");
    const labels = [...reading.matchAll(/<dt class="kicker[^"]*">([^<]*)</g)].map((m) => m[1]);
    assert.deepEqual(labels, ["total", "model", "tool", "approval", "check"]);
    assert.match(reading, /data-tip="approval · 1× · 100% of total"/);
    assert.match(reading, /class="metric quiet[\s"][^>]*data-tip="approval check · 1× · 0% of total"/);
    assert.match(reading, />123h \d\dm \d\ds</, "a multi-day duration is drawn whole");
    assert.doesNotMatch(reading, /wall-clock|own scale/);
  });
});
