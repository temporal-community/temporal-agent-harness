// ABOUTME: Pins what the in-flight reply says the agent is doing, read from the turn's events —
// thinking, running a named tool, waiting on a person, replying, finished — and its timer, which
// counts from the turn's own start event to the newest event, so a reload, a second tab and a
// replay cursor all read the same time and a bad timestamp hides the timer instead of "NaNs".
// The animation has to stop for reduced motion, in the shared shimmer and in the loader.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { buildReplayLog } from "./replayLog.ts";
import {
  formatReplyElapsed,
  replyAnnouncement,
  replyLabel,
  rowHasDetails,
  settledSummary,
  stepHasDetails,
  traceSteps,
  turnProgress
} from "./replyStatus.ts";
import { toolImages } from "./toolImages.ts";

const frame = (event, timestamp, data = {}) => ({
  event,
  data: { type: event, agent_id: "a", turn_number: 1, turn_id: "turn-1", timestamp, ...data }
});
const FRAMES = [
  frame("turn_started", 100),
  frame("model_interaction_started", 100, { model: "gpt" }),
  frame("model_interaction_ended", 101, { model: "gpt" }),
  frame("tool_start", 102, { tool_id: "t1", tool_name: "search_docs", tool_input: { q: "x" } }),
  frame("tool_end", 103, { tool_id: "t1", tool_name: "search_docs", tool_output: "ok" }),
  frame("tool_approval_requested", 104, { tool_id: "t2", tool_name: "refund", tool_input: {} }),
  frame("tool_approval_resolved", 105, { tool_id: "t2", tool_name: "refund", approved: true }),
  frame("model_interaction_started", 106, { model: "gpt" }),
  frame("reply_delta", 107, { text: "Hi" }),
  frame("turn_end", 108)
];
const at = (cursor) => turnProgress(buildReplayLog(FRAMES.slice(0, cursor)).rows);
const labelAt = (cursor) => replyLabel(at(cursor).phase);

describe("what the reply says the agent is doing", () => {
  it("follows the turn's events, step by step", () => {
    assert.equal(labelAt(2), "Thinking", "a model call with no tokens yet");
    assert.equal(labelAt(4), "Running search_docs");
    assert.equal(labelAt(5), "Thinking", "the tool ended, nothing new yet");
    assert.equal(labelAt(6), "Waiting for approval");
    assert.equal(labelAt(8), "Thinking", "approved, and the next model call opened");
    assert.equal(labelAt(9), "Replying");
    assert.equal(labelAt(10), "Reply finished");
    assert.equal(replyAnnouncement(at(2).phase), "Agent is thinking");
    assert.equal(replyAnnouncement(at(4).phase), "Running search_docs");
    assert.equal(replyAnnouncement(at(6).phase), "", "the pending banners announce an approval");
  });

  it("waits for a response, not an approval, when only callbacks are pending", () => {
    const ask = frame("callback_requested", 102, { tool_id: "c1", tool_name: "ask_user", tool_input: {} });
    const gate = frame("tool_approval_requested", 103, { tool_id: "t2", tool_name: "refund", tool_input: {} });
    const progress = (frames) => turnProgress(buildReplayLog([FRAMES[0], FRAMES[1], ...frames]).rows).phase;
    assert.equal(replyLabel(progress([ask])), "Waiting for your response");
    assert.equal(replyAnnouncement(progress([ask])), "", "the queue announces it");
    assert.equal(replyLabel(progress([ask, gate])), "Waiting for approval", "a decision outranks a question");
    const answered = frame("callback_resolved", 104, { tool_id: "c1", tool_name: "ask_user", outcome: "ok" });
    assert.equal(replyLabel(progress([ask, answered])), "Thinking");
  });

  it("counts the turn's tools for the settled summary", () => {
    assert.equal(at(10).tools, 1);
    assert.equal(settledSummary(3, 0), "Ran 3 tools");
    assert.equal(settledSummary(0, 4.4), "Thought for 4s");
    assert.equal(settledSummary(0, 0), "Replied");
  });
});

describe("how long the turn has run", () => {
  it("counts from the turn's start event to the newest event, wherever the cursor is", () => {
    const progress = at(4);
    assert.equal(progress.startedAt, 100);
    assert.equal(formatReplyElapsed(progress.startedAt, 102), "2.0s", "parked at tool_start");
    assert.equal(formatReplyElapsed(100, 112.34), "12.3s");
    assert.equal(formatReplyElapsed(100, 164), "1m 4.0s");
    assert.equal(formatReplyElapsed(100, 100 + 3 * 3600 + 5 * 60), "3h 05m", "past an hour, still sane");
  });

  it("hides rather than printing nonsense for missing or bad timestamps", () => {
    for (const [start, now] of [[null, 100], [100, null], [0, 100], [Number.NaN, 100], [100, Infinity]]) {
      assert.equal(formatReplyElapsed(start, now), "");
    }
    assert.equal(formatReplyElapsed(110, 100), "0.0s", "a clock behind the start does not go negative");
    const unstamped = turnProgress(buildReplayLog([frame("turn_started", 0), frame("model_interaction_started", 50)]).rows);
    assert.equal(unstamped.startedAt, 50, "a zero start falls back to the turn's earliest real time");
  });
});

describe("reduced motion", () => {
  it("stops the shimmer and the pixel wave", () => {
    /* Everything after the file's reduced-motion query opens. */
    const reduced = (path) =>
      readFileSync(new URL(path, import.meta.url), "utf8").split("@media (prefers-reduced-motion: reduce)").slice(1).join("");
    assert.match(reduced("../../app.css"), /\.reply-shimmer \{\s*animation: none;/);
    assert.match(reduced("../components/agent/ReplyStatus.svelte"), /\.pixels > span \{\s*animation: none;/);
  });
});

/* A turn's trace drew one row per raw event — seven for one approved call — with the arguments
   cut off mid-token. It folds into one row per step, and a step is a pure function of the rows,
   so a replay cursor folds the frames up to it exactly as live did. */
describe("a turn's trace, as steps", () => {
  const tool = (id, name, input = { q: "x" }) => ({ tool_id: id, tool_name: name, tool_input: input });
  const judged = (id, name, verdict, evaluator = "jev_evaluator") => [
    frame("auto_approval_evaluation_started", 1, { tool_id: id, tool_name: name, evaluation_id: `e-${id}`, evaluator }),
    frame("auto_approval_evaluation_ended", 1.3, { tool_id: id, tool_name: name, evaluation_id: `e-${id}`, evaluator, verdict, reason: "r", details: {} })
  ];
  const gated = (id, name, verdict, evaluator) => [
    frame("tool_requested", 1, tool(id, name)),
    frame("tool_approval_requested", 1, tool(id, name)),
    ...(evaluator === null ? [] : judged(id, name, verdict, evaluator)),
    frame("tool_approval_resolved", 1.32, { tool_id: id, tool_name: name, approved: verdict === "approve", reason: "r", remember: false })
  ];
  const run = (id, name, end = "tool_end") => [
    frame("tool_start", 1.4, tool(id, name)),
    end === "tool_end"
      ? frame("tool_end", 2, { tool_id: id, tool_name: name, tool_output: '{"ok":true}' })
      : frame("tool_error", 2, { tool_id: id, tool_name: name, message: "Timeout: no answer\nat line 3" })
  ];
  const steps = (frames) => traceSteps(buildReplayLog(frames).rows);
  const only = (frames) => {
    const [step, ...rest] = steps(frames);
    assert.equal(rest.length, 0, `one step, not ${rest.length + 1}`);
    return step;
  };

  it("folds an approved call's seven events into one row that says who approved it", () => {
    const step = only([...gated("t1", "search_flights", "approve"), ...run("t1", "search_flights")]);
    assert.equal(step.rows.length, 7);
    assert.deepEqual([step.kind, step.name, step.state, step.status, step.note], ["tool", "search_flights", "done", "", "approved by jev"]);
    assert.equal(step.durationMs, 1000, "from the request to the result, the decision included");
  });

  it("says auto-approved when the evaluator has no name, and approved when a person decided", () => {
    assert.equal(only([...gated("t1", "a", "approve", ""), ...run("t1", "a")]).note, "auto-approved");
    assert.equal(only([...gated("t1", "a", "approve", null), ...run("t1", "a")]).note, "approved");
    assert.equal(only([...gated("t1", "a", "approve", "pkg.checks.budget_evaluator"), ...run("t1", "a")]).note, "approved by budget");
  });

  it("marks a rejected call and a failed one, with the error's first line and no payload", () => {
    const rejected = only(gated("t1", "book_hotel", "deny"));
    assert.deepEqual([rejected.state, rejected.status], ["rejected", "rejected by jev"]);
    const failed = only(run("t1", "search_flights", "tool_error"));
    assert.deepEqual([failed.state, failed.status, failed.note], ["failed", "failed", "Timeout: no answer"]);
  });

  it("follows a call through its states, frame by frame", () => {
    const frames = [...gated("t1", "a", "approve"), ...run("t1", "a")];
    const at = (cursor) => steps(frames.slice(0, cursor))[0];
    assert.deepEqual([at(1).state, at(1).note], ["requested", "requested"]);
    assert.deepEqual([at(2).state, at(2).status], ["awaiting", "awaiting approval"]);
    assert.deepEqual([at(3).state, at(3).status], ["awaiting", "jev checking"]);
    assert.deepEqual([at(6).state, at(6).note], ["running", "approved by jev"]);
    assert.equal(at(7).state, "done");
    const callback = only([frame("callback_requested", 1, { ...tool("c1", "pick_seat"), output_schema: {} })]);
    assert.deepEqual([callback.state, callback.status], ["awaiting", "waiting on client"]);
  });

  it("nests a Code Mode script's host calls, and a state change made while it ran, under it", () => {
    const script = tool("s1", "run_script", { script: "open_trip()" });
    const host = tool("h1", "open_trip", { request: { city: "Paris" } });
    const result = steps([
      frame("tool_start", 1, script),
      frame("tool_start", 1.1, host),
      frame("state_patch", 1.2, { state_id: "trip_board", version: 1, ops: [{ op: "add", path: "/trips/-", value: 1 }] }),
      frame("tool_end", 1.3, { tool_id: "h1", tool_name: "open_trip", tool_output: "{}" }),
      frame("tool_end", 1.4, { tool_id: "s1", tool_name: "run_script", tool_output: "ok" }),
      frame("state_patch", 1.5, { state_id: "trip_board", version: 2, ops: [] })
    ]);
    assert.deepEqual(
      result.map((step) => [step.name, step.note, step.nested]),
      [
        ["run_script", "", false],
        ["open_trip", "", true],
        ["State changed", "trip_board", true],
        ["State changed", "trip_board", false]
      ]
    );
  });

  it("opens a step whose only payload is an image the tool returned", () => {
    const image = JSON.stringify({ blob_ref: { url: `/api/blobs/${"a".repeat(64)}.bin`, media_type: "image/png" } });
    const step = only([
      frame("tool_start", 1, { tool_id: "i1", tool_name: "image_generation" }),
      frame("tool_end", 2, { tool_id: "i1", tool_name: "image_generation", tool_output: image })
    ]);
    const outputRow = step.rows.findLast((row) => row.output);
    assert.equal(toolImages(outputRow?.output, outputRow?.toolName).length, 1);
    assert.equal(stepHasDetails(step), true);
    assert.equal(rowHasDetails(outputRow), true, "and the Logs row it came from opens too");
  });

  it("draws a model call as one row with its tokens", () => {
    const usage = { input_tokens: 2000, output_tokens: 764, thought_tokens: 0, cached_tokens: 0, tool_use_tokens: 0 };
    const step = only([
      frame("model_interaction_started", 100, { model: "gemini-3.8-flash" }),
      frame("model_interaction_ended", 104.6, { model: "gemini-3.8-flash", usage })
    ]);
    assert.deepEqual([step.kind, step.name, step.note, step.state, step.durationMs], ["model", "gemini-3.8-flash", "2,764 tok", "done", 4600]);
    assert.equal(only([frame("model_interaction_started", 100, { model: "m" })]).state, "running");
  });
});
