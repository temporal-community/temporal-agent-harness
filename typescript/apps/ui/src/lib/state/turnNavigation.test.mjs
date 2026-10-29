// ABOUTME: Asserts the turn controls move, from the one position they are always reached from — the
// exact index of a turn marker. nextTurn() sought `marker.index >= viewIndex`, which matches the
// marker the cursor is standing on, so at the start of any turn it re-seeked to the index it was
// already on and the button did nothing. Both turn buttons leave you exactly there, so the dead
// position was the common one, not an edge.
//   npx vitest run src/lib/state/turnNavigation.test.mjs
//
// Driven by the repo's own mock run rather than a fixture written for this test. The indices are
// whatever realisticQaScenario produces (14 markers over 144 frames, one of them at index 0, which
// is what makes the boundary reachable at all) and every assertion below is stated against
// `run.turnMarkers` as read back from the controller, so a scenario edit moves the check with it
// instead of quietly aiming it at indices that no longer hold markers.
//
// The `>=` -> `>` revert is what this test is calibrated against: with it restored, the per-marker
// case fails on the first marker and the forward walk exhausts its step budget without leaving
// index 0.
//
// Also pins the two properties StepController's jump-to-latest button rests on, since it is
// `aria-disabled` rather than `disabled` and so stays pressable: that `following` means exactly
// `viewIndex === total`, and that pressing it at the live edge changes nothing.
import assert from "node:assert/strict";
import { beforeAll, describe, it } from "vitest";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
/* These are the shipped methods — a copy of nextTurn() in this test could agree with every
   assertion while the app drifted away from both. */
import { realisticQaScenario } from "../mock/scenarios.ts";
import { AgentRunController } from "./agentRun.svelte.ts";

describe("turn navigation", () => {
  let run;
  let markers;
  let total;

  /* `following` is not a mode, and StepController's jump-to-latest button is disabled on it rather
     than announcing itself pressed, which is only honest if the flag means exactly "the cursor is at
     the end". goTo() assigns it that way, and every seek below routes through goTo, so the invariant
     is asserted after each one rather than trusted once. */
  const invariantHolds = (where) =>
    assert.equal(
      run.following,
      run.viewIndex === total,
      `following must mean viewIndex === total (${where}: following=${run.following}, ` +
        `viewIndex=${run.viewIndex}, total=${total})`
    );

  beforeAll(() => {
    installBrowserSurface();

    const api = new Proxy({}, { get: () => async () => [] });
    run = new AgentRunController(api);
    run.sessions = realisticQaScenario.sessions;
    run.session = realisticQaScenario.sessions[0];
    run.frames = realisticQaScenario.frames;

    markers = run.turnMarkers.map((marker) => marker.index);
    total = run.total;
  });

  it("has turns to navigate, at least two indices apart", () => {
    assert.ok(markers.length >= 3, `the scenario must carry turns to navigate (saw ${markers.length})`);
    assert.equal(markers[0], 0, "a run's first turn starts at index 0, which is the boundary case");
    assert.ok(markers.at(-1) < total, "the last marker must leave somewhere for the end fallback to go");

    /* previousTurn() seeks `index < viewIndex - 1`, not `< viewIndex`, so it steps over a marker sitting
       one index behind the cursor. Unreachable here — the tightest gap in this scenario is 3 — but the
       backward assertions below would fail confusingly rather than informatively if a scenario edit ever
       put two turn_starteds on adjacent indices, so the precondition says so out loud. That asymmetry is
       left as found: it is a separate question from the boundary this test exists to pin. */
    const gaps = markers.slice(1).map((index, i) => index - markers[i]);
    assert.ok(
      Math.min(...gaps) >= 2,
      `these assertions assume turns start at least 2 indices apart (tightest gap ${Math.min(...gaps)})`
    );
  });

  // The whole bug, stated once per turn in the run. `>=` matches the marker under the cursor, so every
  // one of these stays put.
  it("the boundary: from a marker's own index, forward must reach the NEXT marker", () => {
    for (let i = 0; i < markers.length - 1; i += 1) {
      run.goTo(markers[i]);
      assert.equal(run.viewIndex, markers[i], "the seek that sets up the case must itself land");
      run.nextTurn();
      assert.equal(
        run.viewIndex,
        markers[i + 1],
        `from the exact index of turn ${i + 1} (index ${markers[i]}), Next turn must advance to the ` +
          `following marker at ${markers[i + 1]} — it stayed at ${run.viewIndex}`
      );
      invariantHolds(`after nextTurn from marker ${markers[i]}`);
    }
  });

  // The `?? this.total` fallback. Also the one place nextTurn is expected to set `following`.
  it("past the last marker, forward runs to the end", () => {
    run.goTo(markers.at(-1));
    run.nextTurn();
    assert.equal(run.viewIndex, total, "from the last turn, Next turn must run to the live edge");
    assert.equal(run.following, true, "arriving at the end is what following means");
    invariantHolds("after nextTurn from the last marker");
  });

  // StepController marks the jump-to-latest button `aria-disabled` while following, which leaves it
  // focusable and therefore pressable — a keyboard user can and will press it. Nothing guards the
  // handler, and this is why: from the live edge jumpToLive() is goTo(total) from total, so the clamp
  // returns the same index, the flag recomputes to the same true, and pause() finds playback already
  // stopped. If this ever fails, the button needs a guard and the comment in StepController that says
  // it does not is wrong.
  it("the live edge absorbs a second press", () => {
    run.goTo(total);
    const snapshot = () => ({
      viewIndex: run.viewIndex,
      following: run.following,
      playing: run.playing,
      total: run.total
    });
    const atEdge = snapshot();
    assert.deepEqual(atEdge, { viewIndex: total, following: true, playing: false, total }, "setup");

    for (let press = 1; press <= 3; press += 1) {
      run.jumpToLive();
      assert.deepEqual(
        snapshot(),
        atEdge,
        `press ${press} of Jump to latest step at the live edge must change nothing`
      );
    }
  });

  // Passes under `>=` too, since a cursor mid-turn has no marker of its own to match. Pinned so a
  // future rewrite cannot fix the boundary by breaking the ordinary case.
  it("from between two markers, forward still reaches the next one", () => {
    const midway = markers[1] + 1;
    assert.ok(midway < markers[2], "the midway probe must stay inside turn 2");
    run.goTo(midway);
    run.nextTurn();
    assert.equal(run.viewIndex, markers[2], "from mid-turn, Next turn must reach the next turn");
  });

  // The failure as a person meets it — holding Next turn down from the start of a session. Under `>=`
  // this never leaves index 0, so the budget is the assertion: it cannot be spent if each press moves.
  it("the forward walk: repeated presses must enumerate the run, not stall", () => {
    run.goTo(0);
    const visited = [0];
    const budget = markers.length + 4;
    for (let press = 0; press < budget && run.viewIndex < total; press += 1) {
      const before = run.viewIndex;
      run.nextTurn();
      assert.ok(
        run.viewIndex > before,
        `press ${press + 1} of Next turn did not move the cursor off index ${before}`
      );
      visited.push(run.viewIndex);
    }
    assert.equal(
      run.viewIndex,
      total,
      `walking forward by turns must reach the end within ${budget} presses (stopped at ` +
        `${run.viewIndex} having visited ${JSON.stringify(visited)})`
    );
    assert.deepEqual(
      visited,
      [...markers, total],
      "the forward walk must land on every turn in order, then the live edge"
    );
  });

  // previousTurn() already had the strict form, so this is the arm that worked. Pinned because the two
  // are only useful as a pair: the fix above is "make forward behave like backward", and that claim
  // stops being checkable the moment backward is unpinned.
  it("backward, from a marker's own index", () => {
    for (let i = markers.length - 1; i > 0; i -= 1) {
      run.goTo(markers[i]);
      run.previousTurn();
      assert.equal(
        run.viewIndex,
        markers[i - 1],
        `from the exact index of turn ${i + 1} (index ${markers[i]}), Previous turn must fall back to ` +
          `the marker at ${markers[i - 1]} — it went to ${run.viewIndex}`
      );
      invariantHolds(`after previousTurn from marker ${markers[i]}`);
    }
  });

  it("the backward walk, and the floor", () => {
    run.goTo(total);
    const budget = markers.length + 4;
    for (let press = 0; press < budget && run.viewIndex > 0; press += 1) {
      const before = run.viewIndex;
      run.previousTurn();
      assert.ok(
        run.viewIndex < before,
        `press ${press + 1} of Previous turn did not move the cursor off index ${before}`
      );
    }
    assert.equal(run.viewIndex, 0, "walking back by turns must reach the first step");

    /* The `?? 0` floor: already home, and it stays there rather than wrapping. */
    run.previousTurn();
    assert.equal(run.viewIndex, 0, "Previous turn at the first step must stay at the first step");
    invariantHolds("at the floor");
  });

  // Only true when both bounds are strict: `>=` returns 0 for the forward leg and the round trip
  // "succeeds" without either press having moved.
  it("the pair round-trips", () => {
    for (let i = 0; i < markers.length - 1; i += 1) {
      run.goTo(markers[i]);
      run.nextTurn();
      run.previousTurn();
      assert.equal(
        run.viewIndex,
        markers[i],
        `Next turn then Previous turn must return to index ${markers[i]}`
      );
    }
  });

  /* Both controls pause: scrubbing by turns is reading, and playback would walk the cursor off
     whatever was being read. */
  it("both controls pause playback", () => {
    run.goTo(0);
    run.play();
    assert.equal(run.playing, true, "the setup must actually be playing");
    run.nextTurn();
    assert.equal(run.playing, false, "Next turn must pause playback");
    run.play();
    run.previousTurn();
    assert.equal(run.playing, false, "Previous turn must pause playback");
    run.pause();
  });
});
