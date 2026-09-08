// ABOUTME: Asserts the graph's focus view removes what has finished and keeps what has not.
// A settled tool leaves the canvas outright and nothing stands in for it — the transcript holds
// what it did — while a tool still running, still queued, or sitting at a human approval gate
// keeps its card. The fold splices runtimeNodeOrder in place and every edge is derived from what
// survives, so the failure this guards hardest against is an edge pointing at a card that is no
// longer drawn.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { kindFromState } from "$lib/components/flow/AgentStateNode.svelte";
import {
  buildAgentGraph,
  cursorHoldSteps,
  settledNearCursor,
  settledToolIdFromFrame
} from "./flowProjection.ts";
import { NO_THOUGHT_SUMMARY } from "./thoughtSummary.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });
const started = frame("turn_started", { user_message: "go", turn_number: 1 });
const modelStarted = frame("model_interaction_started", { model: "gpt" });

/** One tool that ran and finished. */
const settledTool = (id, name = "hub_write") => [
  frame("tool_requested", { tool_id: id, tool_name: name, tool_input: { id } }),
  frame("tool_end", { tool_id: id, tool_name: name, tool_output: "ok" })
];

/** One tool that started and has not come back. */
const runningTool = (id, name = "hub_write") => [
  frame("tool_requested", { tool_id: id, tool_name: name, tool_input: { id } }),
  frame("tool_start", { tool_id: id, tool_name: name })
];

function graphOf(frames, options = {}) {
  return buildAgentGraph(frames, options);
}

const toolCards = (graph) => graph.nodes.filter((node) => node.id.startsWith("tool:"));
const activeOf = (graph) => graph.nodes.filter((node) => node.data.active === true);

/* Two settled calls and one still running: the smallest turn where focus and
   accumulated disagree. */
const mixedTurn = [started, modelStarted, ...settledTool("a"), ...settledTool("b"), ...runningTool("c")];

describe("focus view removes the tools that have finished", () => {
  it("keeps every card when focus is off", () => {
    assert.equal(toolCards(graphOf(mixedTurn)).length, 3, "accumulated draws all three tools");
  });

  it("draws only the tool still running", () => {
    const cards = toolCards(graphOf(mixedTurn, { focus: true }));
    assert.deepEqual(
      cards.map((node) => node.id),
      ["tool:c"],
      "the settled pair leaves the canvas with nothing standing in for it"
    );
  });

  /* The lone settled tool used to be kept, on the argument that a summary card
     reading "×1" cost the same room as the tool it replaced. With no summary
     card to draw, that exception has nothing left to protect. */
  it("removes a settled tool even when it is the only one", () => {
    const graph = graphOf([started, modelStarted, ...settledTool("a")], { focus: true });
    assert.equal(toolCards(graph).length, 0, "one settled tool is still a finished tool");
  });

  /* The failure mode of the whole approach. Edges are built from the surviving
     order, so a fold that dropped an id without dropping its edges would leave
     the canvas drawing arrows into nothing. */
  it("leaves no edge pointing at a card it removed", () => {
    for (const options of [{}, { focus: true }]) {
      const graph = graphOf(mixedTurn, options);
      const drawn = new Set(graph.nodes.map((node) => node.id));
      for (const edge of graph.edges) {
        assert.ok(drawn.has(edge.source), `edge ${edge.id} leaves a node that is not drawn`);
        assert.ok(drawn.has(edge.target), `edge ${edge.id} enters a node that is not drawn`);
      }
    }
  });

  it("still marks exactly one node active, in either view", () => {
    assert.equal(activeOf(graphOf(mixedTurn)).length, 1, "accumulated has one live card");
    assert.equal(activeOf(graphOf(mixedTurn, { focus: true })).length, 1, "so does focus");
  });

  /* Without the fallback the turn loses its active node at exactly the moment
     its last tool settles, because the pointer is left aimed at a removed card.
     The model is what the run goes back to, which is the card a reader wants
     lit while the reply streams. */
  it("moves the active mark to the model when the last tool settles off the canvas", () => {
    const graph = graphOf([started, modelStarted, ...settledTool("a"), ...settledTool("b")], {
      focus: true
    });
    assert.deepEqual(
      activeOf(graph).map((node) => node.id),
      ["model"]
    );
  });
});

describe("focus view never hides a tool that has not finished", () => {
  /* The bug a `status !== "running"` predicate would have shipped: `awaiting` is
     not running and is emphatically not settled, and it is the single card a
     reader most needs on screen. */
  it("keeps a tool sitting at a human approval gate", () => {
    const graph = graphOf(
      [
        started,
        ...settledTool("a"),
        ...settledTool("b"),
        frame("tool_requested", { tool_id: "ask", tool_name: "task_ask", tool_input: { q: "ok?" } }),
        frame("tool_approval_requested", {
          tool_id: "ask",
          tool_name: "task_ask",
          tool_input: { q: "ok?" }
        })
      ],
      { focus: true }
    );
    const cards = toolCards(graph);
    assert.equal(cards.length, 1, "the approval gate is the one card left standing");
    assert.equal(cards[0].id, "tool:ask");
    assert.equal(cards[0].data.state, "awaiting");
  });

  it("keeps a tool the model has requested but not yet started", () => {
    const graph = graphOf(
      [
        started,
        ...settledTool("a"),
        ...settledTool("b"),
        frame("tool_requested", { tool_id: "q", tool_name: "hub_write", tool_input: {} })
      ],
      { focus: true }
    );
    assert.deepEqual(
      toolCards(graph).map((node) => node.id),
      ["tool:q"],
      "queued is not finished"
    );
  });

  /* Nothing here is finished, so nothing here is hidden — however wide the fan gets.
     This is a rule, not an accident of small fans: asked for it directly, the Gemini
     agents fire eight calls in one response, which is past the cap of six that an
     earlier draft used to fold the overflow into a summary card. That draft was cut
     anyway. A tool still running is work the reader is waiting on, and the summary it
     would collapse into counted those calls as "pending" — a worse answer than the
     eight live cards, which say which ones. Ten here, deliberately above the measured
     eight. */
  it("keeps every card of a wide parallel fan", () => {
    const frames = [started];
    for (let index = 0; index < 10; index += 1) frames.push(...runningTool(`t${index}`));
    assert.equal(toolCards(graphOf(frames, { focus: true })).length, 10);
  });

  /* The trap this whole fold is one wrong predicate away from. A granted approval
     tones the card "done" and names it "approval granted" while the call has yet to
     run, so anything keying off tone hides a tool that is about to execute. Only the
     three genuinely finished paths — tool_end, tool_error, a REFUSED approval — call
     markToolSettled, which is why the fold reads that and not the tone. */
  it("keeps a tool whose approval was granted but which has not run", () => {
    const graph = graphOf(
      [
        started,
        modelStarted,
        frame("tool_requested", { tool_id: "g", tool_name: "rm", tool_input: {} }),
        frame("tool_approval_requested", { tool_id: "g", tool_name: "rm", tool_input: {} }),
        frame("tool_approval_resolved", { tool_id: "g", tool_name: "rm", approved: true })
      ],
      { focus: true }
    );
    const cards = toolCards(graph);
    assert.deepEqual(cards.map((n) => n.id), ["tool:g"], "granted is not finished");
    assert.equal(cards[0].data.tone, "done", "and it really is toned done — that is the trap");
  });
});

/* Code Mode host calls hang off `codeModeChildren` rather than `runtimeNodeOrder`, so
   the fold that splices that order never reached them: mid-run, while the host was
   still executing, its finished children stayed on the canvas beside its running ones.
   Caught by replaying prefixes of a real Monty subagent run — the end state looked
   right because a settled host takes its children with it. */
describe("focus view removes settled Code Mode children too", () => {
  const codeModeTurn = [
    started,
    modelStarted,
    frame("tool_requested", {
      tool_id: "host",
      tool_name: "run_travel_code",
      tool_input: { script: "book()" }
    }),
    frame("tool_start", { tool_id: "host", tool_name: "run_travel_code" }),
    ...settledTool("kid1", "book_flight"),
    ...runningTool("kid2", "get_trip_summary")
  ];

  it("keeps every child while focus is off", () => {
    assert.deepEqual(
      toolCards(graphOf(codeModeTurn)).map((n) => n.id).sort(),
      ["tool:host", "tool:kid1", "tool:kid2"]
    );
  });

  it("drops the finished child and keeps the running one and the host", () => {
    assert.deepEqual(
      toolCards(graphOf(codeModeTurn, { focus: true })).map((n) => n.id).sort(),
      ["tool:host", "tool:kid2"]
    );
  });

  /* The container is sized from its child count, so a child that stops being drawn
     has to stop being counted as well or the box keeps a hole where it was. */
  it("shrinks the host container to the children it still draws", () => {
    const hostOf = (graph) => graph.nodes.find((n) => n.id === "tool:host");
    const accumulated = hostOf(graphOf(codeModeTurn));
    const focused = hostOf(graphOf(codeModeTurn, { focus: true }));
    assert.ok(
      focused.data.nodeHeight <= accumulated.data.nodeHeight,
      `focus box must not grow (${focused.data.nodeHeight} vs ${accumulated.data.nodeHeight})`
    );
  });

  /* The script had been getting 26px — one clipped line behind a scrollbar — because
     the header was a flat number the result body had to fit inside. The card carries
     that height to the CSS now, so this pins the two together: whatever room the host
     reserves for its script, the first host call has to start below it. */
  it("reserves readable room for the script above its host calls", () => {
    const graph = graphOf(codeModeTurn);
    const host = graph.nodes.find((n) => n.id === "tool:host");
    /* Host calls are placed absolutely, at the host's origin plus an offset, so
       the gap that matters is measured against the host rather than the canvas. */
    const children = graph.nodes.filter((n) => n.id === "tool:kid1" || n.id === "tool:kid2");

    assert.ok(
      host.data.resultHeight >= 100,
      `script body must be readable, got ${host.data.resultHeight}px`
    );
    assert.equal(children.length, 2, "expected the host to draw its calls inside it");
    for (const child of children) {
      const gap = child.position.y - host.position.y;
      assert.ok(
        gap >= host.data.resultHeight,
        `host call ${child.id} sits ${gap}px down, overlapping a ${host.data.resultHeight}px script`
      );
    }
  });
});

/* The card picks its chip by sniffing the words already on it, and falls back to
   the tone when none of them match. Imported rather than reimplemented, and
   asserted here rather than through a rendered node, which cannot render outside
   SvelteFlow's context. */
describe("a card toned done is drawn as finished, not as idle", () => {
  /* The neutral chip means nothing has happened yet, which is the wrong thing to
     say about a card toned done. The Output node has always had this gap. */
  it("settles the Output card", () => {
    assert.equal(kindFromState("reply available", "done"), "complete");
  });

  it("still lets the state string outrank the tone", () => {
    assert.equal(kindFromState("running", "done"), "thinking");
    assert.equal(kindFromState("awaiting", "done"), "approval");
  });

  it("leaves a card with nothing to say on the neutral chip", () => {
    assert.equal(kindFromState("idle", undefined), "idle");
  });
});

/* A call that starts and finishes between two glances was a card that had never
   been on screen to read: focus view took it away the moment it earned the answer
   it was drawn to give. `linger` is the caller saying "this one only just
   finished", and the beat it buys is the whole point — the card keeps its own
   DONE, so nothing here checks for a caption. Timing lives in the controller;
   what is asserted here is only that a named tool is held and then let go. */
describe("focus view holds a tool that has only just finished", () => {
  const justFinished = [started, modelStarted, ...settledTool("a"), ...settledTool("b")];
  const heldCards = (linger) =>
    toolCards(graphOf(justFinished, { focus: true, linger: new Set(linger) })).map((n) => n.id);

  it("draws the held tool and no other settled one", () => {
    assert.deepEqual(heldCards(["a"]), ["tool:a"], "b finished a while ago and is gone");
  });

  it("lets it go once the caller stops holding it", () => {
    assert.deepEqual(heldCards([]), [], "an empty hold is the fold as it was");
  });

  it("still shows the state the reader was held there to see", () => {
    const graph = graphOf(justFinished, { focus: true, linger: new Set(["a"]) });
    assert.equal(graph.nodes.find((n) => n.id === "tool:a").data.state, "done");
  });

  /* Same failure the fold itself guards against, in the other direction: a card
     put back into the order has to be wired back into the flow with it. */
  it("leaves no edge pointing at a card it is not drawing", () => {
    const graph = graphOf(justFinished, { focus: true, linger: new Set(["a"]) });
    const drawn = new Set(graph.nodes.map((node) => node.id));
    for (const edge of graph.edges) {
      assert.ok(drawn.has(edge.source), `edge ${edge.id} leaves a node that is not drawn`);
      assert.ok(drawn.has(edge.target), `edge ${edge.id} enters a node that is not drawn`);
    }
  });

  /* Children live outside runtimeNodeOrder, so they needed the hold wiring
     separately — exactly as they needed the fold wiring separately. */
  it("holds a Code Mode host call the same way", () => {
    const frames = [
      started,
      frame("tool_requested", {
        tool_id: "host",
        tool_name: "run_travel_code",
        tool_input: { script: "book()" }
      }),
      frame("tool_start", { tool_id: "host", tool_name: "run_travel_code" }),
      ...settledTool("kid1", "book_flight")
    ];
    const idsWith = (linger) =>
      toolCards(graphOf(frames, { focus: true, linger: new Set(linger) }))
        .map((n) => n.id)
        .sort();
    assert.deepEqual(idsWith(["kid1"]), ["tool:host", "tool:kid1"]);
    assert.deepEqual(idsWith([]), ["tool:host"], "and drops it when the hold ends");
  });
});

/* What the controller times the hold off. It has to agree with the builder about
   what "finished" means, or it holds a card that is still running or fails to
   hold one that stopped — which is why it is one exported function and not a
   second opinion. */
describe("the settle a hold is timed from", () => {
  it("names the tool on the three frames that finish one", () => {
    assert.equal(settledToolIdFromFrame(frame("tool_end", { tool_id: "a", tool_name: "x" })), "a");
    assert.equal(settledToolIdFromFrame(frame("tool_error", { tool_id: "b", tool_name: "x" })), "b");
    assert.equal(
      settledToolIdFromFrame(
        frame("tool_approval_resolved", { tool_id: "c", tool_name: "x", approved: false })
      ),
      "c",
      "a refusal is the end of that call"
    );
  });

  /* The trap the fold is one wrong predicate away from, asserted here too: this
     card is toned done while the call has yet to run. */
  it("does not call a granted approval finished", () => {
    assert.equal(
      settledToolIdFromFrame(
        frame("tool_approval_resolved", { tool_id: "g", tool_name: "rm", approved: true })
      ),
      null
    );
  });

  it("ignores the frames that are not about a tool ending", () => {
    assert.equal(settledToolIdFromFrame(started), null);
    assert.equal(settledToolIdFromFrame(frame("tool_start", { tool_id: "a", tool_name: "x" })), null);
  });
});

/* The other half of the hold, and the half the first version missed: a finished run
   being read back. Nothing settles live there, so every tool in it blinked out as the
   cursor stepped past its end frame — the whole complaint, in the view someone reviewing
   a run actually sits in. Measured in steps rather than seconds because the reader sets
   the pace: a card cannot be held "for 1.2 seconds" on a frame someone is parked on. */
describe("a run being read back holds a tool for one step of the cursor", () => {
  const ended = (id) => ({ frame: frame("tool_end", { tool_id: id, tool_name: "glob" }) });
  const noise = () => ({ frame: frame("reply_delta", { text: "..." }) });
  const heldAt = (timeline) => [...settledNearCursor(timeline)].sort();

  it("holds the tool the cursor has just stepped onto", () => {
    assert.deepEqual(heldAt([noise(), ended("a")]), ["a"]);
  });

  it("lets it go once the cursor has stepped past the window", () => {
    const after = (steps) => heldAt([ended("a"), ...Array.from({ length: steps }, noise)]);
    assert.deepEqual(after(cursorHoldSteps - 1), ["a"], "still within the beat");
    assert.deepEqual(after(cursorHoldSteps), [], "and then it is gone");
  });

  /* Each settle is its own frame, so "what just finished" is always one call. Two
     that end back to back each get their own step rather than piling up — which is
     the point of one step over several, and what keeps a finished card from sitting
     next to a live one. */
  it("holds only the tool that finished on this step, not the one before it", () => {
    assert.deepEqual(heldAt([ended("a"), ended("b")]), ["b"]);
    assert.deepEqual(heldAt([ended("a")]), ["a"], "and a lands on its own step");
  });

  it("holds nothing where nothing finished", () => {
    assert.deepEqual(heldAt([noise(), noise(), noise()]), []);
  });

  /* A cursor at the very start has no steps behind it to look back through. */
  it("copes with a cursor shorter than the window", () => {
    assert.deepEqual(heldAt([]), []);
    assert.deepEqual(heldAt([ended("a")]), ["a"]);
  });

  /* Deterministic from the cursor alone, which is what lets a reader step back and
     forward and land on the same picture rather than on whatever a timer was doing. */
  it("answers the same for the same cursor every time", () => {
    const timeline = [noise(), ended("a"), noise()];
    assert.deepEqual(heldAt(timeline), heldAt(timeline));
  });

  /* The trap, again: a granted approval is not a settle, and read-back must not
     invent a finished card for a call that was about to run. */
  it("does not hold a tool whose approval was merely granted", () => {
    const granted = {
      frame: frame("tool_approval_resolved", { tool_id: "g", tool_name: "rm", approved: true })
    };
    assert.deepEqual(heldAt([granted]), []);
  });
});

describe("focus view drops a thought trace that has finished", () => {
  const thinking = [started, modelStarted, frame("thought_summary", { delta: { content: { text: "weigh it up" } } })];
  const thought = [...thinking, frame("model_interaction_ended", { model: "gpt" })];
  const hasReasoning = (graph) => graph.nodes.some((node) => node.id === "reasoning");

  it("keeps the card while fragments are still arriving", () => {
    assert.ok(hasReasoning(graphOf(thinking, { focus: true })));
  });

  it("removes it once the thinking is over", () => {
    assert.ok(hasReasoning(graphOf(thought)), "accumulated keeps the finished trace");
    assert.ok(!hasReasoning(graphOf(thought, { focus: true })));
  });

  /* The reason the "did a thought card exist" flag is read before the fold runs.
     Asked afterwards it answers about the wrong thing, and a run that thought
     without streaming text would report no thinking at all rather than saying
     the summary never arrived. */
  it("still reports a thought that streamed no text", () => {
    const graph = graphOf(
      [started, modelStarted, frame("thought_summary", { delta: {} }), frame("model_interaction_ended", { model: "gpt" })],
      { focus: true }
    );
    const model = graph.nodes.find((node) => node.id === "model");
    const summary = model.data.context.find((section) => section.label === "Thought summary");
    assert.equal(summary?.text, NO_THOUGHT_SUMMARY);
  });
});
