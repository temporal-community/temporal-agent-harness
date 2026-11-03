// ABOUTME: Asserts a held key's repeats coalesce to one cursor move per frame, and that the net move
// is exactly what the same presses would have made one at a time.
//   npx vitest run src/lib/state/heldStepping.test.mjs
//
// The oracle is the controller itself: every expected index comes from replaying the same keys as
// separate presses, so the check holds the coalesced path to the stepping rules (reply runs crossed
// in one stop, clamping at both ends) without restating them.
import assert from "node:assert/strict";
import { beforeAll, beforeEach, describe, it } from "vitest";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { realisticQaScenario } from "../mock/scenarios.ts";
import { AgentRunController } from "./agentRun.svelte.ts";
import { applyReplayAction } from "./replayHotkeys.ts";

describe("held step keys", () => {
  let run;
  let surface;
  let flushRaf;
  let rafQueue;
  let middle;

  /* Where the same moves land when each is its own press. */
  const oneAtATime = (from, moves) => {
    run.goTo(from);
    for (const move of moves) run[move]();
    const landed = run.viewIndex;
    run.goTo(from);
    return landed;
  };

  /* Auto-repeat starts after the OS delay, long after the first keydown's frame, so a burst is all
     repeats and the first of them finds no frame pending. */
  const hold = (moves) => moves.forEach((move) => run[move](true));

  beforeAll(() => {
    ({ flushRaf, rafQueue } = installBrowserSurface({ controllableRaf: true }));
    const api = new Proxy({}, { get: () => async () => [] });
    run = new AgentRunController(api);
    run.sessions = realisticQaScenario.sessions;
    run.session = realisticQaScenario.sessions[0];
    run.frames = realisticQaScenario.frames;
    middle = Math.floor(run.total / 2);
    surface = { run, helpOpen: false };
  });

  beforeEach(() => {
    flushRaf();
    flushRaf();
    run.goTo(middle);
  });

  it("the first press applies at once, so a single step reads back synchronously", () => {
    const expected = oneAtATime(middle, ["stepBack"]);
    run.stepBack();
    assert.equal(run.viewIndex, expected);
    run.goTo(middle);
    run.stepForward(true);
    assert.equal(
      run.viewIndex,
      oneAtATime(middle, ["stepForward"]),
      "a repeat with no frame pending applies at once too"
    );
  });

  it("repeats within one frame land together on the next frame, net of every press", () => {
    const moves = Array(6).fill("stepBack");
    const expected = oneAtATime(middle, moves);
    const afterFirst = oneAtATime(middle, ["stepBack"]);
    hold(moves);
    assert.equal(run.viewIndex, afterFirst, "only the first press has moved the cursor");
    flushRaf();
    assert.equal(run.viewIndex, expected, "the frame applies all six");
    flushRaf();
    assert.equal(rafQueue.length, 0, "a frame with nothing pending closes the window");
  });

  it("a reversal inside one frame nets out in order", () => {
    const moves = ["stepBack", "stepBack", "stepForward", "stepBack", "stepForward", "stepForward", "stepForward"];
    const expected = oneAtATime(middle, moves);
    hold(moves);
    flushRaf();
    assert.equal(run.viewIndex, expected);
  });

  it("each repeat clamps on its own at the first event", () => {
    const moves = [...Array(5).fill("stepBack"), "stepForward"];
    const expected = oneAtATime(1, moves);
    assert.ok(expected > 0, "the step forward off the floor must count");
    run.goTo(1);
    hold(moves);
    flushRaf();
    assert.equal(run.viewIndex, expected);
  });

  it("and at the live edge, where landing follows it again", () => {
    const moves = [...Array(5).fill("stepForward"), "stepBack"];
    const from = run.total - 1;
    const expected = oneAtATime(from, moves);
    assert.ok(expected < run.total);
    run.goTo(from);
    hold(Array(5).fill("stepForward"));
    flushRaf();
    assert.equal(run.viewIndex, run.total);
    assert.equal(run.following, true, "a coalesced move onto the end follows it like any other");
    run.goTo(from);
    hold(moves);
    flushRaf();
    assert.equal(run.viewIndex, expected);
  });

  it("frames keep applying while repeats keep landing", () => {
    const moves = Array(9).fill("stepBack");
    const expected = oneAtATime(middle, moves);
    run.stepBack(true);
    for (let frame = 0; frame < 4; frame += 1) {
      run.stepBack(true);
      run.stepBack(true);
      flushRaf();
    }
    assert.equal(run.viewIndex, expected);
  });

  it("a fresh press after repeats applies at once, from where they were headed", () => {
    const moves = ["stepBack", "stepBack", "stepBack", "stepForward"];
    const expected = oneAtATime(middle, moves);
    hold(moves.slice(0, 3));
    run.stepForward();
    assert.equal(run.viewIndex, expected);
    flushRaf();
    assert.equal(run.viewIndex, expected, "the pending frame does not replay the repeats");
  });

  it("an absolute seek drops what the repeats had pending", () => {
    hold(Array(4).fill("stepForward"));
    run.goTo(3);
    flushRaf();
    assert.equal(run.viewIndex, 3);
  });

  it("the turn keys coalesce the same way", () => {
    const moves = Array(3).fill("previousTurn");
    const from = run.total;
    const expected = oneAtATime(from, moves);
    run.goTo(from);
    hold(moves);
    flushRaf();
    assert.equal(run.viewIndex, expected);
  });

  it("the key handler passes the event's repeat flag through", () => {
    const expected = oneAtATime(middle, ["stepBack", "stepBack", "stepBack"]);
    const afterFirst = oneAtATime(middle, ["stepBack"]);
    applyReplayAction("stepBack", surface, true);
    applyReplayAction("stepBack", surface, true);
    applyReplayAction("stepBack", surface, true);
    assert.equal(run.viewIndex, afterFirst, "the later repeats wait for the frame");
    flushRaf();
    assert.equal(run.viewIndex, expected);
  });
});
