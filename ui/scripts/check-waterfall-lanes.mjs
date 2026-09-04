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
//   node ui/scripts/check-waterfall-lanes.mjs

import assert from "node:assert/strict";
import "./libAlias.mjs";
import "./svelteLoader.mjs";

const { buildStepTimeline, playheadFraction, turnScale } = await import(
  "../src/lib/state/stepTimeline.ts"
);
/* From the component, the way check-status-note.mjs reads statusKind() out of
   TranscriptPanel: the rule belongs where it is used, and a copy of it here would
   pass forever while the real one rotted. */
const { COLLAPSE_THRESHOLD, turnExpanded, turnHeldOpen } = await import(
  "../src/lib/components/flow/LatencyWaterfall.svelte"
);

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

// --- lanes are named, and a kind owns its rows -------------------------------------------------
{
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
}

// --- a kind with nothing to show costs no rows -------------------------------------------------
{
  clock = 0;
  const timeline = buildStepTimeline([
    frame("turn_started", { user_message: "go" }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    ((clock = 8), frame("model_interaction_ended", {}))
  ]);
  const [turn] = timeline.turns;
  assert.equal(turn.laneCount, 1, "a model-only turn is one row, not three empty swimlanes");
  assert.equal(turn.spans[0].lane, 0, "and that row is the top one");
}

// --- per-turn scale ----------------------------------------------------------------------------
// The whole point of the change: two turns of wildly different length each fill their own row.
{
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
}

// --- a subagent that outlives its parent turn still fits ---------------------------------------
{
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
}

// --- playhead: index in, fraction of the row's wall-clock out ----------------------------------
{
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
}

// --- threshold collapse, and the two turns it is forbidden to fold ------------------------------
{
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

  const none = new Set();
  /* An index past every turn's frames, so `turnHeldOpen` can only be answering the
     failure question — with the cursor parked in a turn, that turn passes for the
     other reason and the two rules stop being separable. */
  const PARKED = 10_000;

  assert.equal(COLLAPSE_THRESHOLD, 12, "the collapse threshold is twelve turns");

  // Below the line nothing folds, which is what keeps today's short sessions untouched.
  {
    const timeline = run(COLLAPSE_THRESHOLD);
    for (const turn of timeline.turns) {
      assert.ok(
        turnExpanded(turn, PARKED, timeline.turns.length, none),
        `at exactly ${COLLAPSE_THRESHOLD} turns every row still draws its tracks; turn ` +
          `${turn.turnNumber} did not`
      );
    }
  }

  // One turn over it, and the rows that have nothing to say fold.
  {
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
  }

  // --- auto-expand rule 1: the turn that went wrong ---------------------------------------------
  {
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
  }

  // A span still running is the other thing worth stopping on, and it is not an error tone.
  {
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
  }

  // A subagent's failure is the parent turn's failure: it is drawn inside that row.
  {
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
  }

  // --- auto-expand rule 2: the turn under the playhead ------------------------------------------
  {
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
  }
}

console.log(
  "check-waterfall-lanes: kind lanes, per-turn scale, the playhead mapping and the collapse " +
    "threshold's two auto-expand rules hold"
);
