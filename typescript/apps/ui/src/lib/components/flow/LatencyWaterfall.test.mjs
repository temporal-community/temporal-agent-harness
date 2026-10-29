// ABOUTME: Asserts the three pieces of arithmetic the latency waterfall now rests on, all of which
// used to be something else and none of which is visible in a screenshot until it is already wrong.
// (1) Lanes are FIXED BY KIND — model, then tool, then approval — so the same kind sits on the same
// row in every turn; the greedy packer this replaced assigned rows by overlap alone, which is how
// three concurrent approvals became three unrelated blobs. (2) A turn's scale is its OWN duration,
// not the run's longest, which is what stops a 45s turn rendering as hairlines beside an 8m35s one
// — and it must still cover a subagent that outlives its parent's last frame, or that subagent's
// bars paint off the end of the track. (3) The playhead maps the player's event INDEX onto the
// row's wall-clock through the (index, timestamp) pairs the spans already carry, and refuses to
// draw at all outside the turn — the two domains are bridged, never conflated.
// (4) Which rows the threshold collapse is allowed to fold, which is the one rule here that can
// hide information: a run over twelve turns draws proportion bars instead of tracks, EXCEPT for a
// turn holding the playhead or holding a failure. A collapsed view that folds away the turn that
// went wrong is worse than no collapse at all, and it fails silently — the reader simply never
// sees it — so the two exemptions are asserted rather than trusted to the markup.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { scaleScenario } from "$lib/mock/scaleScenarios.ts";
import {
  buildStepTimeline,
  denseLanes,
  indexAtFraction,
  playheadFraction,
  spanNear,
  turnScale
} from "$lib/state/stepTimeline.ts";
/* From the component: the rule belongs where it is used, and a copy of it here would
   pass forever while the real one rotted. */
import LatencyWaterfall, {
  axisTicks,
  COLLAPSE_THRESHOLD,
  turnExpanded,
  turnHeldOpen,
  turnWorthDrawing
} from "./LatencyWaterfall.svelte";

const source = readFileSync(new URL("./LatencyWaterfall.svelte", import.meta.url), "utf8");
let clock = 0;
const frame = (event, data) => ({
  event,
  data: { type: event, agent_id: "root", turn_number: 1, timestamp: clock, ...data }
});

/* One parent turn holding, in this order of arrival: a model call, then two tools that overlap
   each other, then two approvals that overlap each other. Under the old packer the second tool
   and the second approval would land on whatever lane happened to be free. */
function overlappingTurn() {
  const frames = [];
  const at = (ts, f) => {
    clock = ts;
    frames.push(f());
  };

  at(0, () => frame("turn_started", { user_message: "go" }));
  at(0, () => frame("model_interaction_started", { model: "gpt-5.1" }));
  at(10, () => frame("model_interaction_ended", {}));
  at(10, () => frame("tool_start", { tool_id: "t1", tool_name: "read_file" }));
  at(12, () => frame("tool_start", { tool_id: "t2", tool_name: "write_file" }));
  at(20, () => frame("tool_end", { tool_id: "t1" }));
  at(24, () => frame("tool_end", { tool_id: "t2" }));
  at(24, () => frame("tool_approval_requested", { tool_id: "a1", tool_name: "shell" }));
  at(25, () => frame("tool_approval_requested", { tool_id: "a2", tool_name: "network" }));
  at(40, () => frame("tool_approval_resolved", { tool_id: "a1", approved: true }));
  at(45, () => frame("tool_approval_resolved", { tool_id: "a2", approved: true }));

  return buildStepTimeline(frames);
}

const laneOf = (turn, label) => turn.spans.find((span) => span.label.includes(label))?.lane;

describe("the latency waterfall", () => {
  // --- lanes are named, and a kind owns its rows -------------------------------------------------
  it("names its lanes, and gives a kind its own rows", () => {
    const [turn] = overlappingTurn().turns;

    assert.equal(laneOf(turn, "gpt-5.1"), 0, "the model lane is the top one");
    assert.equal(laneOf(turn, "read_file"), 1, "tool starts where the model block ends");
    assert.equal(
      laneOf(turn, "write_file"),
      2,
      "a second tool overlapping the first takes a SECOND tool row rather than painting over it"
    );
    assert.equal(
      laneOf(turn, "approval · shell"),
      3,
      "approvals start below every tool row, however many the tools needed"
    );
    assert.equal(
      laneOf(turn, "approval · network"),
      4,
      "and overlapping approvals stay legible the same way tools do"
    );
    assert.equal(turn.laneCount, 5, "five rows: 1 model + 2 tool + 2 approval");

    // The property that a greedy packer cannot give: lane order is the kind order, always.
    const order = { model: 0, tool: 1, approval: 2 };
    const byLane = [...turn.spans].sort((a, b) => a.lane - b.lane);
    for (let i = 1; i < byLane.length; i += 1) {
      assert.ok(
        order[byLane[i - 1].kind] <= order[byLane[i].kind],
        `lanes must run model → tool → approval top to bottom; row ${byLane[i].lane} is ` +
          `${byLane[i].kind} under a ${byLane[i - 1].kind}`
      );
    }
  });

  // --- a kind with nothing to show costs no rows -------------------------------------------------
  it("spends no rows on a kind with nothing to show", () => {
    clock = 0;
    const timeline = buildStepTimeline([
      frame("turn_started", { user_message: "go" }),
      frame("model_interaction_started", { model: "gpt-5.1" }),
      ((clock = 8), frame("model_interaction_ended", {}))
    ]);
    const [turn] = timeline.turns;
    assert.equal(turn.laneCount, 1, "a model-only turn is one row, not three empty swimlanes");
    assert.equal(turn.spans[0].lane, 0, "and that row is the top one");
  });

  it("projects callback waits through the approval lane", () => {
    clock = 0;
    const timeline = buildStepTimeline([
      frame("turn_started", { user_message: "go" }),
      frame("callback_requested", { tool_id: "callback", tool_name: "ask_user", tool_input: {} }),
      ((clock = 12), frame("callback_resolved", { tool_id: "callback", tool_name: "ask_user", result: {} }))
    ]);
    const wait = timeline.turns[0].spans[0];
    assert.equal(wait.kind, "approval");
    assert.equal(wait.label, "callback · ask_user");
    assert.equal(wait.durationSeconds, 12);
  });

  // --- per-turn scale ----------------------------------------------------------------------------
  // The whole point of the change: two turns of wildly different length each fill their own row.
  it("scales every turn to its own duration", () => {
    clock = 0;
    const frames = [];
    const push = (ts, f) => {
      clock = ts;
      frames.push(f());
    };
    const turnFrame = (event, turnNumber, data) => ({
      event,
      data: { type: event, agent_id: "root", turn_number: turnNumber, timestamp: clock, ...data }
    });

    push(0, () => turnFrame("turn_started", 1, { user_message: "short" }));
    push(0, () => turnFrame("model_interaction_started", 1, { model: "gpt-5.1" }));
    push(45, () => turnFrame("model_interaction_ended", 1, {}));
    push(100, () => turnFrame("turn_started", 2, { user_message: "long" }));
    push(100, () => turnFrame("model_interaction_started", 2, { model: "gpt-5.1" }));
    push(615, () => turnFrame("model_interaction_ended", 2, {}));

    const [short, long] = buildStepTimeline(frames).turns;
    assert.equal(turnScale(short), 45, "the 45s turn is scaled to 45s");
    assert.equal(turnScale(long), 515, "and the long turn to its own 515s");

    // What a shared scale did to the short turn, stated as the number it was: under
    // `max(maxTurnDuration)` the 45s bar occupied under a tenth of its row.
    const sharedWidth = (45 / 515) * 100;
    assert.ok(sharedWidth < 10, `sanity: the old shared scale gave it ${sharedWidth.toFixed(1)}%`);
    assert.equal(
      (short.spans[0].durationSeconds / turnScale(short)) * 100,
      100,
      "on its own scale the same bar fills the row"
    );

    assert.equal(
      turnScale({ turnNumber: 9, startTs: 0, endTs: 0, durationSeconds: 0, subagentTurns: [] }),
      1,
      "a zero-length turn floors to a unit scale rather than dividing by zero"
    );
  });

  // --- a subagent that outlives its parent turn still fits ---------------------------------------
  it("still fits a subagent that outlives its parent turn", () => {
    const turn = {
      turnNumber: 1,
      startTs: 100,
      endTs: 160,
      durationSeconds: 60,
      spans: [],
      subagentTurns: [{ startTs: 110, endTs: 400, spans: [] }]
    };
    assert.equal(
      turnScale(turn),
      300,
      "the row is scaled to the last thing on it, or the child's bars run off the end"
    );
  });

  // --- playhead: index in, fraction of the row's wall-clock out ----------------------------------
  it("takes an index and hands back a fraction of the row's wall-clock", () => {
    const [turn] = overlappingTurn().turns;
    const scale = turnScale(turn);
    const model = turn.spans.find((span) => span.kind === "model");

    assert.equal(
      playheadFraction(turn, model.startIndex),
      0,
      "at the turn's first frame the playhead is at the row's left edge"
    );

    const atModelEnd = playheadFraction(turn, model.endIndex);
    assert.ok(
      Math.abs(atModelEnd - 10 / scale) < 1e-9,
      `the model's end frame reads its own timestamp, not a share of the frame count ` +
        `(got ${atModelEnd}, wanted ${10 / scale})`
    );

    // Between two known frames the reading is interpolated rather than snapped, which is the
    // only reason the line moves smoothly while the transport plays.
    const mid = playheadFraction(turn, model.startIndex + 0.5);
    assert.ok(mid > 0 && mid < atModelEnd, `a half-step lands between the two (got ${mid})`);

    for (const fraction of turn.spans.map((span) => playheadFraction(turn, span.startIndex))) {
      assert.ok(fraction >= 0 && fraction <= 1, `every reading stays on the row (got ${fraction})`);
    }

    // Outside the turn there is nothing honest to draw, and one turn's row must not carry a line
    // for a cursor sitting in another turn's frames.
    const last = Math.max(...turn.spans.map((span) => span.endIndex));
    assert.equal(playheadFraction(turn, 0), null, "before the turn's first frame: no line");
    assert.equal(playheadFraction(turn, last + 1), null, "after its last: no line");
    assert.equal(
      playheadFraction({ ...turn, spans: [], subagentTurns: [] }, 1),
      null,
      "and a turn with no measured spans has nothing to map through"
    );
  });

  it("maps a row's wall-clock back to its event index", () => {
    const span = {
      id: "inverse",
      turnNumber: 1,
      kind: "model",
      label: "model",
      tone: "done",
      startTs: 10,
      endTs: 50,
      durationSeconds: 40,
      startIndex: 3,
      endIndex: 7,
      lane: 0,
      ongoing: false
    };
    const turn = {
      role: "parent",
      turnNumber: 1,
      startTs: 10,
      endTs: 50,
      durationSeconds: 40,
      preview: "",
      spans: [span],
      laneCount: 1,
      subagentTurns: [],
      historyGap: false
    };

    for (let index = span.startIndex; index <= span.endIndex; index += 1) {
      assert.equal(indexAtFraction(turn, playheadFraction(turn, index)), index);
    }
    assert.equal(indexAtFraction(turn, -1), span.startIndex, "fractions before the row clamp left");
    assert.equal(indexAtFraction(turn, 2), span.endIndex, "fractions after the row clamp right");

    const zero = {
      ...turn,
      endTs: 10,
      durationSeconds: 0,
      spans: [{ ...span, startTs: 10, endTs: 10, durationSeconds: 0 }]
    };
    assert.equal(indexAtFraction(zero, 0), span.startIndex, "a zero-duration row starts at its first event");
    assert.equal(indexAtFraction(zero, 1), span.endIndex, "and its far edge reaches its last event");
  });

  /* A stroke here boxed every concurrent span in white, and because `.bar.active`
     outranks the global :focus-visible ring, a focused active bar lost its ring too. */
  it("marks the span at the cursor with the selection tint, leaving outline to focus", () => {
    const rule = source.match(/\.bar\.active\s*\{([^}]*)\}/)?.[1] ?? "";
    assert.match(rule, /box-shadow:[^;]*var\(--selected-bg\)/, "the active span is tinted");
    assert.doesNotMatch(rule, /outline|border/, "and never stroked, which would also hide the focus ring");
  });

  it("wires pointer capture scrubbing to every ruler and track", () => {
    assert.equal(
      source.match(/onpointerdown=\{\(event\) => startScrub\(event, (?:turn|track\.turn)\)\}/g)?.length,
      2,
      "the ruler and shared parent/subagent track both start the same scrub gesture"
    );
    for (const handler of ["onpointermove={moveScrub}", "onpointerup={stopScrub}", "onpointercancel={stopScrub}"]) {
      assert.equal(source.split(handler).length - 1, 2, `${handler} stays on both scrub surfaces`);
    }
    assert.match(source, /element\.setPointerCapture\(event\.pointerId\)/);
    assert.match(source, /element\.releasePointerCapture\(event\.pointerId\)/);
    assert.match(
      source,
      /\.axis,\s*\.track\s*\{\s*direction: ltr;/,
      "a row's wall-clock still runs left to right inside an RTL page"
    );
  });

  it("draws one playhead through the ruler, parent lanes, and subagent lanes", () => {
    clock = 0;
    const timeline = buildStepTimeline([
      { frame: frame("turn_started", { user_message: "delegate" }), role: "parent" },
      { frame: frame("model_interaction_started", { model: "gpt-5.1" }), role: "parent" },
      ((clock = 2),
      {
        frame: frame("tool_start", { tool_id: "s1", tool_name: "search" }),
        role: "subagent",
        workflowId: "child",
        label: "Child",
        parentTurnNumber: 1
      }),
      ((clock = 8),
      {
        frame: frame("tool_end", { tool_id: "s1" }),
        role: "subagent",
        workflowId: "child",
        label: "Child",
        parentTurnNumber: 1
      }),
      ((clock = 10),
      { frame: frame("model_interaction_ended", {}), role: "parent" })
    ]);
    const [turn] = timeline.turns;
    const cursor = turn.spans[0].startIndex;
    const html = render(LatencyWaterfall, {
      props: { timeline, viewIndex: cursor, onScrub() {} }
    }).body;

    assert.equal(turn.subagentTurns.length, 1, "sanity: the row has parent and subagent tracks");
    assert.equal(
      html.match(/class="playhead(?:\s|")/g)?.length,
      1,
      "the whole turn row shares one line"
    );
    assert.match(
      html,
      new RegExp(`left: calc\\(18px \\+ \\(100% - 18px\\) \\* ${playheadFraction(turn, cursor)}\\)`),
      "the shared line keeps the turn's existing playhead position"
    );
  });

  it("marks a turn's cues at their playhead positions", () => {
    const timeline = overlappingTurn();
    const [turn] = timeline.turns;
    const cue = {
      id: "marker-approval",
      index: turn.spans.find((span) => span.kind === "approval").startIndex,
      turnNumber: turn.turnNumber,
      tone: "approval",
      label: "Approval requested"
    };
    const html = render(LatencyWaterfall, {
      props: {
        timeline,
        viewIndex: cue.index,
        anomalyMarkers: [cue],
        onScrub() {}
      }
    }).body;

    assert.match(
      html,
      new RegExp(
        `class="cue approval [^"]+" style="--at: ${playheadFraction(turn, cue.index)}" ` +
          `aria-label="Jump to Approval requested, turn ${turn.turnNumber}"`
      ),
      "the cue button keeps the lane's name and sits at its event's wall-clock fraction"
    );
  });

  // --- threshold collapse, and the two turns it is forbidden to fold ------------------------------
  describe("threshold collapse, and the two turns it is forbidden to fold", () => {
    /* `turns` turns of model → tool, with the tool in `errorTurn` failing and the tool in
       `ongoingTurn` never closing. Both are the shapes a reader is scrolling this pane to find. */
    function run(turns, { errorTurn = null, ongoingTurn = null } = {}) {
      clock = 0;
      const frames = [];
      const push = (event, turnNumber, data = {}) => {
        frames.push({
          event,
          data: { type: event, agent_id: "root", turn_number: turnNumber, timestamp: clock, ...data }
        });
      };
      for (let turn = 1; turn <= turns; turn += 1) {
        push("turn_started", turn, { user_message: `turn ${turn}` });
        push("model_interaction_started", turn, { model: "gpt-5.1" });
        clock += 5;
        push("model_interaction_ended", turn);
        push("tool_start", turn, { tool_id: `t${turn}`, tool_name: "read_file" });
        clock += 4;
        if (turn === errorTurn) push("tool_error", turn, { tool_id: `t${turn}`, message: "boom" });
        else if (turn !== ongoingTurn) push("tool_end", turn, { tool_id: `t${turn}` });
        clock += 1;
      }
      return buildStepTimeline(frames);
    }

    /* Three turns, one of each shape the rule sorts: a turn whose spans relate to each
       other, a turn that is one model call and nothing else, and a turn that happened
       at a single instant. The last two are the rows the screenshot was full of. */
    function trivialRunFrames() {
      clock = 0;
      const frames = [];
      const push = (event, turnNumber, data = {}) =>
        frames.push({
          event,
          data: { type: event, agent_id: "root", turn_number: turnNumber, timestamp: clock, ...data }
        });

      push("turn_started", 1, { user_message: "rich" });
      push("model_interaction_started", 1, { model: "gpt-5.1" });
      clock += 5;
      push("model_interaction_ended", 1);
      push("tool_start", 1, { tool_id: "a", tool_name: "read_file" });
      clock += 4;
      push("tool_end", 1, { tool_id: "a" });

      push("turn_started", 2, { user_message: "one call" });
      push("model_interaction_started", 2, { model: "gpt-5.1" });
      clock += 13;
      push("model_interaction_ended", 2);

      /* No clock movement at all, which is what a 0s turn is. */
      push("turn_started", 3, { user_message: "instant" });
      push("model_interaction_started", 3, { model: "gpt-5.1" });
      push("model_interaction_ended", 3);
      push("tool_start", 3, { tool_id: "b", tool_name: "read_file" });
      push("tool_end", 3, { tool_id: "b" });

      return frames;
    }

    /** An index the playhead reaches inside a given turn, whatever its spans. */
    const insideTurn = (turn) => turn.startIndex;

    const none = new Set();
    /* An index past every turn's frames, so `turnHeldOpen` can only be answering the
       failure question — with the cursor parked in a turn, that turn passes for the
       other reason and the two rules stop being separable. */
    const PARKED = 10_000;

    it("folds at twelve turns", () => {
      assert.equal(COLLAPSE_THRESHOLD, 12, "the collapse threshold is twelve turns");
    });

    // Below the line a turn with a shape still draws it, however many such turns there are.
    it("draws every shaped row below the line", () => {
      const timeline = run(COLLAPSE_THRESHOLD);
      for (const turn of timeline.turns) {
        assert.ok(
          turnWorthDrawing(turn),
          `sanity: the fixture's turns have a model and a tool to relate; turn ` +
            `${turn.turnNumber} does not`
        );
        assert.ok(
          turnExpanded(turn, PARKED, timeline.turns.length, none),
          `at exactly ${COLLAPSE_THRESHOLD} turns a row with a shape still draws its tracks; ` +
            `turn ${turn.turnNumber} did not`
        );
      }
    });

    /* The second reason to fold, and the one the count never reached: a short session
       whose rows are mostly empty. Three turns is nowhere near the threshold, so every
       fold here is the shape rule doing it. */
    it("folds a row with no shape even in a session too short to fold", () => {
      const timeline = buildStepTimeline(trivialRunFrames());
      const [rich, single, instant] = timeline.turns;
      const count = timeline.turns.length;
      assert.equal(count, 3, "sanity: the fixture built three turns");
      assert.ok(count < COLLAPSE_THRESHOLD, "sanity: and stayed well under the threshold");

      assert.ok(turnWorthDrawing(rich), "a turn whose spans relate to each other is worth a row");
      assert.ok(turnExpanded(rich, PARKED, count, none), "and it draws them");

      assert.equal(
        turnWorthDrawing(single),
        false,
        "one span has nothing to relate it to, so the row buys nothing"
      );
      assert.equal(turnExpanded(single, PARKED, count, none), false, "and it folds");

      assert.equal(
        turnWorthDrawing(instant),
        false,
        "a turn of no duration has no ruler to place anything along"
      );
      assert.equal(turnExpanded(instant, PARKED, count, none), false, "and it folds too");

      // Folded by shape is still a turn the reader can open, and the one they open only.
      assert.ok(
        turnExpanded(single, PARKED, count, new Set([single.turnNumber])),
        "a folded-by-shape turn opens by hand"
      );
      assert.equal(
        turnExpanded(instant, PARKED, count, new Set([single.turnNumber])),
        false,
        "and opening it leaves its neighbour folded"
      );

      /* The guarantee that matters most for the new rule: the rows it folds are exactly
         the rows a reader would never miss — until one of them is where the cursor is,
         or where something broke. Both outrank it, in a session too short to fold. */
      const inside = insideTurn(instant);
      assert.ok(
        turnHeldOpen(instant, inside),
        "an empty turn holding the cursor is held open against the shape rule"
      );
      assert.ok(turnExpanded(instant, inside, count, none), "and it draws its tracks");
    });

    // One turn over it, and the rows that have nothing to say fold.
    it("folds the uneventful rows one turn over the line", () => {
      const timeline = run(COLLAPSE_THRESHOLD + 1);
      const count = timeline.turns.length;
      assert.equal(count, 13, "sanity: the fixture built the turns it was asked for");
      for (const turn of timeline.turns) {
        assert.equal(
          turnExpanded(turn, PARKED, count, none),
          false,
          `over the threshold an uneventful turn collapses; turn ${turn.turnNumber} stayed open`
        );
      }
      // And the reader can still open one by hand, which is the only state the set holds.
      assert.ok(
        turnExpanded(timeline.turns[3], PARKED, count, new Set([4])),
        "a turn the reader opened stays open"
      );
      assert.equal(
        turnExpanded(timeline.turns[3], PARKED, count, new Set([5])),
        false,
        "and opening one turn does not open its neighbour"
      );
    });

    // --- auto-expand rule 1: the turn that went wrong ---------------------------------------------
    it("holds open the turn that went wrong", () => {
      const timeline = run(20, { errorTurn: 14 });
      const count = timeline.turns.length;
      const failed = timeline.turns.find((turn) => turn.turnNumber === 14);
      assert.ok(
        failed.spans.some((span) => span.tone === "error"),
        "sanity: the fixture's turn 14 really does hold a failed tool"
      );
      assert.ok(
        turnHeldOpen(failed, PARKED),
        "a turn holding a failure is held open regardless of the threshold"
      );
      assert.ok(
        turnExpanded(failed, PARKED, count, none),
        "and so it draws its tracks inside an otherwise collapsed run"
      );
      assert.equal(
        turnHeldOpen(
          timeline.turns.find((turn) => turn.turnNumber === 13),
          PARKED
        ),
        false,
        "while the turn beside it, which did not fail, still folds"
      );
    });

    // A span still running is the other thing worth stopping on, and it is not an error tone.
    it("holds open a turn with something still in flight", () => {
      const timeline = run(20, { ongoingTurn: 9 });
      const live = timeline.turns.find((turn) => turn.turnNumber === 9);
      assert.ok(
        live.spans.some((span) => span.ongoing),
        "sanity: the fixture's turn 9 holds a span that never closed"
      );
      assert.ok(
        turnHeldOpen(live, PARKED),
        "a turn with something still in flight is held open too — `ongoing`, not `tone`, says so"
      );
    });

    // A subagent's failure is the parent turn's failure: it is drawn inside that row.
    it("holds open the parent row of a subagent's failure", () => {
      clock = 0;
      const timeline = buildStepTimeline([
        { frame: frame("turn_started", { user_message: "delegate" }), role: "parent" },
        {
          frame: frame("tool_start", { tool_id: "s1", tool_name: "spawn" }),
          role: "subagent",
          workflowId: "child",
          label: "Child",
          parentTurnNumber: 1
        },
        ((clock = 6),
        {
          frame: frame("tool_error", { tool_id: "s1", message: "boom" }),
          role: "subagent",
          workflowId: "child",
          label: "Child",
          parentTurnNumber: 1
        })
      ]);
      const [parent] = timeline.turns;
      assert.equal(parent.spans.length, 0, "sanity: the parent turn owns no spans of its own");
      assert.ok(
        turnHeldOpen(parent, PARKED),
        "a failure that happened in a nested subagent still holds its parent row open, or the " +
          "collapse hides the one row the failure is drawn on"
      );
    });

    // --- auto-expand rule 2: the turn under the playhead ------------------------------------------
    it("holds open the turn under the playhead, and only that one", () => {
      const timeline = run(20);
      const count = timeline.turns.length;
      const watched = timeline.turns.find((turn) => turn.turnNumber === 16);
      const inside = watched.spans[0].startIndex;
      assert.notEqual(
        playheadFraction(watched, inside),
        null,
        "sanity: that index really is inside turn 16"
      );
      assert.ok(
        turnHeldOpen(watched, inside),
        "the turn the cursor is in is held open, so stepping never lands inside a folded row"
      );
      assert.ok(turnExpanded(watched, inside, count, none), "and it draws its tracks");

      // Exactly one row at a time: every other turn folds while the cursor sits in this one.
      const open = timeline.turns.filter((turn) => turnExpanded(turn, inside, count, none));
      assert.deepEqual(
        open.map((turn) => turn.turnNumber),
        [16],
        "and it is the only row held open — the rest of the run stays folded"
      );
    });
  });

  // --- at scale: rows in view, and a strip where a lane is too crowded for bars --------------------
  describe("at scale", () => {
    const draw = (timeline, viewIndex = 0) =>
      render(LatencyWaterfall, { props: { timeline, viewIndex, onScrub() {} } }).body;

    it("draws a screenful of a 12,500-turn run, each row placed in the whole", () => {
      const timeline = buildStepTimeline(scaleScenario("frames100k").frames);
      const html = draw(timeline);
      const drawn = html.match(/class="turn-row/g)?.length ?? 0;
      assert.ok(timeline.turns.length > 12_000, "sanity: the fixture is long");
      assert.ok(drawn > 0 && drawn < 40, `drew ${drawn} of ${timeline.turns.length} rows`);
      assert.match(html, /role="feed"/, "the rows are a feed");
      assert.match(html, /aria-setsize="12500"/, "and each says how many turns the run holds");
    });

    it("draws a crowded lane as one strip, keeping the cursor's spans as bars", () => {
      const timeline = buildStepTimeline(scaleScenario("dense5k").frames);
      const dense = timeline.turns.find((turn) => turn.spans.length > 4_000);
      const cursor = dense.spans[2_000].startIndex;
      const html = draw(timeline, cursor);
      const strips = html.match(/class="strip /g)?.length ?? 0;
      const bars = html.match(/class="bar /g)?.length ?? 0;
      assert.ok(strips >= 2, `the model and tool lanes are strips (got ${strips})`);
      assert.ok(bars > 0 && bars < 20, `${dense.spans.length} spans drew ${bars} bars`);
      assert.match(html, /class="bar [^"]*active[^"]*over-strip/, "the span at the cursor is still drawn as itself");
      const future = Number(html.match(/--future: ([\d.]+)%/)?.[1]);
      assert.ok(future > 0 && future < 100, `and the strip fades past the cursor, from ${future}%`);
    });

    /* Unisolated, an Arabic tool name pulled its own duration into its run and read "0 · name",
       and in a right-to-left page an emoji name read "run · 1s_👩🏽‍💻". */
    it("isolates a span's name from its duration, whichever way the name runs", () => {
      clock = 0;
      const html = draw(
        buildStepTimeline([
          frame("turn_started", { user_message: "go" }),
          frame("tool_start", { tool_id: "a", tool_name: "قراءة_الملف" }),
          ((clock = 3), frame("tool_end", { tool_id: "a" })),
          frame("tool_start", { tool_id: "b", tool_name: "👩🏽‍💻_run" }),
          ((clock = 5), frame("tool_end", { tool_id: "b" }))
        ])
      );
      assert.match(html, /<bdi>قراءة_الملف<\/bdi> · \d/);
      assert.match(html, /<bdi>👩🏽‍💻_run<\/bdi> · \d/);
    });

    it("bins a crowded lane per pixel, and points at the longest span under the pointer", () => {
      const span = (i, startTs, endTs, extra = {}) => ({
        id: `s${i}`, turnNumber: 1, kind: "tool", label: "t", tone: "done", startTs, endTs,
        durationSeconds: endTs - startTs, startIndex: i, endIndex: i, lane: 1, ongoing: false, ...extra
      });
      const lane = Array.from({ length: 1_000 }, (_, i) =>
        span(i, i, i + 1, i === 500 ? { tone: "error" } : i === 999 ? { ongoing: true } : {})
      );
      const model = span(-1, 0, 1_000, { kind: "model", lane: 0 });
      const [strip, ...rest] = denseLanes([model, ...lane], 0, 1_000, 200);
      assert.equal(rest.length, 0, "only the crowded lane is binned");
      assert.equal(strip.lane, 1);
      assert.equal(strip.fill, "M0 0h200v20h-200z", "back-to-back spans paint one run");
      assert.equal(strip.error, "M100 0h1v20h-1z", "the failure keeps its own pixel");
      assert.equal(strip.ongoing, "M199 0h1v20h-1z", "and so does the span still running");
      assert.equal(denseLanes([model, ...lane], 0, 1_000, 3_000).length, 1, "bars under 4px wide are still a strip");
      assert.equal(denseLanes([model, ...lane], 0, 1_000, 4_000).length, 0, "with room, every lane keeps its bars");

      const sparse = [span(0, 0, 1), span(1, 1, 4), span(2, 4, 5), span(3, 10, 11)];
      assert.equal(spanNear(sparse, 0.5, 4.5)?.id, "s1", "the longest span under the pointer wins");
      assert.equal(spanNear(sparse, 10.2, 10.3)?.id, "s3");
      assert.equal(spanNear(sparse, 6, 9), null, "and empty track points at nothing");
    });
  });

  /* At a 320px pane a hidden overflow label made `.turns` 12,091px wide, the subagent label
     column pushed each subagent track off its parent's time axis, and five ticks a row
     printed "2h 00m 00s3h 00m 00s". */
  describe("at the minimum pane width", () => {
    const rule = (selector) =>
      [...source.matchAll(new RegExp(`\\n\\s*${selector.replace(/\./g, "\\.")}\\s*\\{([^}]*)\\}`, "g"))]
        .map((match) => match[1])
        .join("\n");

    it("keeps every overflow label inside its track", () => {
      assert.match(rule(".track"), /overflow-x: clip;/, "an opacity-0 label is still layout");
      assert.match(rule(".bar.unlabelled .bar-text"), /max-width: min\([^)]*cqi\)/);
    });

    it("lets the track take the row below the Timeline's breakpoint", () => {
      assert.match(rule(".waterfall"), /container: waterfall \/ inline-size;/);
      assert.match(
        source,
        /@container waterfall \(max-width: 559px\)\s*\{\s*\.turn-row\s*\{\s*grid-template-columns: minmax\(0, 1fr\);/
      );
    });

    it("puts no px floor beside a subagent track, so it shares its parent's x range", () => {
      for (const selector of [".subagent-stack", ".subagent-row", ".subagent-label"]) {
        assert.doesNotMatch(rule(selector), /minmax\(\d+px|min-width: [1-9]|padding-left|border-left/, selector);
      }
      assert.match(rule(".subagent-row"), /min-width: 0;/);
      assert.match(rule(".subagent-label"), /min-width: 0;/);
      assert.match(rule(".track-wrap"), /grid-template-columns: 16px minmax\(0, 1fr\);/);
      assert.match(rule(".turn-body"), /min-width: 0;/);
    });

    it("positions a subagent's band in percent of the parent turn's scale", () => {
      clock = 0;
      const timeline = buildStepTimeline([
        { frame: frame("turn_started", { user_message: "delegate" }), role: "parent" },
        { frame: frame("model_interaction_started", { model: "gpt-5.1" }), role: "parent" },
        ((clock = 25),
        { frame: frame("tool_start", { tool_id: "s1", tool_name: "search" }), role: "subagent", workflowId: "child", label: "Child", parentTurnNumber: 1 }),
        ((clock = 75),
        { frame: frame("tool_end", { tool_id: "s1" }), role: "subagent", workflowId: "child", label: "Child", parentTurnNumber: 1 }),
        ((clock = 100), { frame: frame("model_interaction_ended", {}), role: "parent" })
      ]);
      const html = render(LatencyWaterfall, { props: { timeline, viewIndex: 0, onScrub() {} } }).body;
      assert.match(html, /class="subagent-band[^"]*" style="left: 25%; width: 50%"/);
    });

    it("thins the ruler to the track's width with the Timeline's tick rule", () => {
      const width = (tick) => `${tick}`.length * 40;
      const wide = axisTicks(3_600, 1_200, () => 60);
      const narrow = axisTicks(3_600, 256, () => 60);
      assert.ok(narrow.ticks.length < wide.ticks.length, "a narrower track draws fewer ticks");
      assert.equal(narrow.ticks.at(-1), 3_600, "and still ends at the row's own duration");
      for (const ticks of [axisTicks(12_250, 256, width).ticks, axisTicks(1_334, 256, width).ticks]) {
        const scale = ticks.at(-1);
        const boxes = ticks.map((tick, i) => {
          const x = (tick / scale) * 256;
          const w = width(tick);
          const left = i === 0 ? x : i === ticks.length - 1 ? x - w : x - w / 2;
          return [left, left + w];
        });
        for (let i = 1; i < boxes.length; i++) assert.ok(boxes[i][0] >= boxes[i - 1][1], `tick ${i} clears tick ${i - 1}`);
      }
      assert.equal(axisTicks(100, 1_000, () => 30).gap, 10, "guides sit on the nice step, not the end tick");
      assert.doesNotMatch(source, /niceTimeTicks\(scale, 5\)/, "no fixed tick count per row");
    });
  });
});
