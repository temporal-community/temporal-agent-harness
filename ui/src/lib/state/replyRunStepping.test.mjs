// ABOUTME: Asserts a streamed reply is ONE stop and ONE log row in the shipped controller — the
// thing a reader actually meets, as opposed to buildReplyRuns in isolation (replyRuns.test.mjs).
//   npx vitest run src/lib/state/replyRunStepping.test.mjs
//
// Driven by the repo's own mock run rather than a fixture written for this test, and every
// assertion is stated against a run discovered from `run.replyRuns`, so a scenario edit moves the
// check with it instead of aiming it at indices that no longer hold a reply.
//
// Playback is pinned here too, and pinned as DIFFERENT: it advances a frame at a time on purpose,
// so a recording still shows the reply arriving the way it arrived live. That difference is easy
// to "tidy up" into a call to stepForward(), which is why it is written down as a test.
import assert from "node:assert/strict";
import { beforeAll, describe, it } from "vitest";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { realisticQaScenario } from "../mock/scenarios.ts";
import { AgentRunController } from "./agentRun.svelte.ts";
import { buildReplayLog } from "./replayLog.ts";

describe("collapsed reply runs", () => {
  let run;
  let streamed;
  let total;

  beforeAll(() => {
    installBrowserSurface();

    const api = new Proxy({}, { get: () => async () => [] });
    run = new AgentRunController(api);
    run.sessions = realisticQaScenario.sessions;
    run.session = realisticQaScenario.sessions[0];
    run.frames = realisticQaScenario.frames;

    streamed = run.replyRuns.find((item) => item.frameCount > 1);
    total = run.total;
  });

  it("the scenario streams a reply in more than one chunk", () => {
    assert.ok(
      streamed,
      "the mock must contain a multi-chunk reply for any of this to be exercised; " +
        `saw runs of ${JSON.stringify(run.replyRuns.map((item) => item.frameCount))}`
    );
    assert.ok(streamed.startIndex > 1, "the run needs a frame before it to step from");
    assert.ok(streamed.endIndex < total, "and one after it, so the far side is reachable");
  });

  it("one press forward crosses the whole run", () => {
    run.goTo(streamed.startIndex - 1);
    run.stepForward();
    assert.equal(
      run.viewIndex,
      streamed.endIndex,
      `stepping into a ${streamed.frameCount}-chunk reply must land past all of it, at the state ` +
        "after the reply — not on its first chunk"
    );
  });

  it("one press back leaves it whole, landing before its first chunk", () => {
    run.goTo(streamed.endIndex);
    run.stepBack();
    assert.equal(
      run.viewIndex,
      streamed.startIndex - 1,
      "stepping back out of a reply must clear it, not walk back through it chunk by chunk"
    );
  });

  it("the pair round-trips", () => {
    run.goTo(streamed.startIndex - 1);
    run.stepForward();
    run.stepBack();
    assert.equal(run.viewIndex, streamed.startIndex - 1);
  });

  it("a lone chunk is still its own stop", () => {
    const lone = run.replyRuns.find((item) => item.frameCount === 1);
    assert.ok(lone, "the mock must also carry a single-chunk reply");
    run.goTo(lone.startIndex - 1);
    run.stepForward();
    assert.equal(
      run.viewIndex,
      lone.startIndex,
      "a reply that arrived in one chunk is an ordinary event and must not be skipped over"
    );
  });

  it("the run draws one log row, carrying every chunk's text", () => {
    const rows = run.fullReplayLog.rows.filter(
      (row) => row.index >= streamed.startIndex && row.index <= streamed.endIndex
    );
    const replyRows = rows.filter((row) => row.event === "reply_delta");

    assert.equal(
      replyRows.length,
      1,
      `${streamed.frameCount} chunks must draw one row, not ${replyRows.length}`
    );
    assert.equal(replyRows[0].body, streamed.text, "and it carries the whole reply");
    assert.equal(replyRows[0].runStartIndex, streamed.startIndex);
    assert.equal(
      replyRows[0].index,
      streamed.endIndex,
      "the row is addressed at the end of the run, which is where a step lands"
    );
  });

  it("a cursor anywhere inside the run reads as that one event", () => {
    for (let index = streamed.startIndex; index <= streamed.endIndex; index += 1) {
      run.goTo(index);
      assert.equal(
        run.currentLogRow?.runStartIndex,
        streamed.startIndex,
        `scrubbing onto chunk ${index} must still read as the collapsed reply, not as nothing`
      );
    }
  });

  // Requirement the feature is only useful if it holds: the same events must group the same way
  // whether they were watched arriving or replayed afterwards.
  it("live and replay agree on the structure", () => {
    const replayed = buildReplayLog(run.replayTimeline).rows;
    const live = buildReplayLog(run.replayTimeline.slice(0, streamed.endIndex)).rows;

    assert.deepEqual(
      live.map((row) => [row.id, row.index]),
      replayed.slice(0, live.length).map((row) => [row.id, row.index]),
      "a log built at the live edge of the run must match the replayed one row for row"
    );
  });

  it("mid-run, the row is the reply so far rather than the whole of it", () => {
    const midIndex = streamed.endIndex - 1;
    const partial = buildReplayLog(run.replayTimeline.slice(0, midIndex)).rows.at(-1);

    assert.equal(partial.event, "reply_delta", "the setup must land mid-reply");
    assert.ok(
      streamed.text.startsWith(partial.body) && partial.body.length < streamed.text.length,
      "a run still streaming shows what has arrived, as a prefix of what eventually will"
    );
    assert.equal(
      partial.id,
      run.fullReplayLog.rows.find((row) => row.runStartIndex === streamed.startIndex).id,
      "and it keeps its identity as it grows, so the tick updates in place instead of remounting"
    );
  });

  it("playback still advances one frame at a time", () => {
    run.goTo(streamed.startIndex - 1);
    run.play();
    assert.equal(run.playing, true, "the setup must actually be playing");
    run.pause();

    /* The timer's body, called directly: a test that waited on the interval would be timing the
       playback speed rather than the step size. */
    run.goTo(run.viewIndex + 1);
    assert.equal(
      run.viewIndex,
      streamed.startIndex,
      "playback lands on the run's FIRST chunk — the typing is the point of a recording"
    );
  });
});
