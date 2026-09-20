// ABOUTME: Asserts an auto approval evaluation draws as its OWN card, wired to the gated call by
// a single edge, and that opening it shows the EVALUATION rather than the tool.
//
// The first version of this folded the evaluation events into the gated tool's runtime, which
// looked right on the canvas and was wrong the moment anyone clicked: the inspector showed the
// tool's input and output — the one thing an approval card is not about. The evaluator is also
// separate work running somewhere else, so a state of the tool node was the wrong shape for it
// regardless of what the inspector showed.
//
// The cancellation path is asserted here too, because it is the case with no other surface: when
// a human answers while the evaluator is still thinking the harness cancels it and publishes
// `superseded`, and a reader needs to see an approval check that stopped rather than one that
// failed or one that silently vanished.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildAgentGraph } from "./flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });
const started = frame("turn_started", { user_message: "go", turn_number: 1 });

const gatedCall = [
  frame("tool_requested", { tool_id: "t1", tool_name: "refund", tool_input: { amount: 20 } }),
  frame("tool_approval_requested", {
    tool_id: "t1",
    tool_name: "refund",
    tool_input: { amount: 20 }
  })
];

const evaluationFrame = (event, extra = {}) =>
  frame(event, {
    tool_id: "t1",
    tool_name: "refund",
    evaluation_id: "e1",
    evaluator: "jev_evaluator",
    ...extra
  });

const evaluationCards = (graph) => graph.nodes.filter((n) => n.id.startsWith("approval:"));
const toolCards = (graph) => graph.nodes.filter((n) => n.id.startsWith("tool:"));
const sectionText = (node, label) =>
  node.data.context?.find((section) => section.label === label)?.text;

describe("the auto approval evaluation as its own node", () => {
  it("draws a card of its own, joined to the gated call by one edge", () => {
    const graph = buildAgentGraph([
      started,
      ...gatedCall,
      evaluationFrame("auto_approval_evaluation_started")
    ]);

    const [card] = evaluationCards(graph);
    assert.ok(card, "the evaluation should have a node of its own");
    assert.equal(card.id, "approval:e1");
    // Titled by the EVALUATOR; the tool it judged is context, not the headline.
    assert.equal(card.data.title, "jev_evaluator");
    assert.equal(card.data.state, "evaluating");
    assert.match(card.data.subtitle, /refund/);

    const [tool] = toolCards(graph);
    const joins = graph.edges.filter(
      (e) => e.source === tool.id && e.target === card.id
    );
    assert.equal(joins.length, 1, "exactly one line between the call and its approval check");

    // The tool card is NOT repurposed as the approval: it is still the gated call.
    assert.equal(tool.data.title, "refund");
    assert.equal(tool.data.state, "awaiting");
  });

  it("opens onto the evaluation, not onto the tool call", () => {
    const graph = buildAgentGraph([
      started,
      ...gatedCall,
      evaluationFrame("auto_approval_evaluation_ended", {
        verdict: "escalate",
        reason: "confidence 61% < 80%",
        details: { confidence: 0.61, thresholds: { min_confidence: 0.8 } }
      })
    ]);

    const [card] = evaluationCards(graph);
    /* Past tense, and not by accident: AgentStateNode picks its status chip by matching
       substrings like "approved"/"denied", so the raw verdict words would fall through
       every rule to the neutral idle chip — a DENIED call drawn as if nothing happened. */
    assert.equal(card.data.state, "escalated");
    assert.equal(sectionText(card, "Evaluator"), "jev_evaluator");
    assert.equal(sectionText(card, "Verdict"), "escalate");
    assert.equal(sectionText(card, "Reason"), "confidence 61% < 80%");
    assert.match(sectionText(card, "Evaluator reasoning"), /min_confidence/);
    // The regression this file exists for: the tool's own payload must not be what an
    // approval card shows.
    assert.equal(sectionText(card, "Tool input"), undefined);
    assert.equal(sectionText(card, "Tool output"), undefined);
  });

  it("shows a cancelled evaluator as cancelled, not as failed", () => {
    const graph = buildAgentGraph([
      started,
      ...gatedCall,
      evaluationFrame("auto_approval_evaluation_started"),
      frame("tool_approval_resolved", {
        tool_id: "t1",
        tool_name: "refund",
        approved: true,
        reason: "human got there first"
      }),
      evaluationFrame("auto_approval_evaluation_superseded", { verdict: null })
    ]);

    const [card] = evaluationCards(graph);
    assert.equal(card.data.state, "cancelled");
    /* Neutral rather than error-toned: nothing went wrong. The answer arrived from
       somewhere else and the evaluator was stopped, which is the design working. */
    assert.equal(card.data.tone, "neutral");
    assert.match(card.data.detail, /decided first/);
  });

  it("keeps a failed evaluator distinct from a cancelled one", () => {
    const graph = buildAgentGraph([
      started,
      ...gatedCall,
      evaluationFrame("auto_approval_evaluation_started"),
      evaluationFrame("auto_approval_evaluation_error", {
        message: "Activity task failed: typesafe is down"
      })
    ]);

    const [card] = evaluationCards(graph);
    assert.equal(card.data.state, "failed");
    assert.equal(card.data.tone, "error");
    assert.match(card.data.detail, /typesafe is down/);
  });

  it("draws one card per evaluator when a call is judged more than once", () => {
    const chained = (id, evaluator) =>
      frame("auto_approval_evaluation_started", {
        tool_id: "t1",
        tool_name: "refund",
        evaluation_id: id,
        evaluator
      });
    const graph = buildAgentGraph([
      started,
      ...gatedCall,
      chained("e1", "cheap_check"),
      chained("e2", "jev_evaluator")
    ]);

    assert.deepEqual(
      evaluationCards(graph)
        .map((n) => n.data.title)
        .sort(),
      ["cheap_check", "jev_evaluator"]
    );
    // Two cards, two lines — the evaluation_id keys them apart, not the shared tool_id.
    assert.equal(graph.edges.filter((e) => e.target.startsWith("approval:")).length, 2);
  });
});

describe("dismissing the evaluation once its gate is decided", () => {
  const escalated = [
    started,
    ...gatedCall,
    evaluationFrame("auto_approval_evaluation_started"),
    evaluationFrame("auto_approval_evaluation_ended", {
      verdict: "escalate",
      reason: "confidence 61% < 80%",
      details: {}
    })
  ];
  const humanApproved = frame("tool_approval_resolved", {
    tool_id: "t1",
    tool_name: "refund",
    approved: true,
    reason: "fine by me"
  });
  const toolRan = [
    frame("tool_start", { tool_id: "t1", tool_name: "refund" }),
    frame("tool_end", { tool_id: "t1", tool_name: "refund", tool_output: "refunded:20" })
  ];

  it("keeps the card up while the gate is still open", () => {
    assert.equal(evaluationCards(buildAgentGraph(escalated)).length, 1);
  });

  it("drops it the moment a human decides, not when the tool finishes", () => {
    /* The bug this covers: the card sat there for the whole of the tool's run. Its subject
       is an OPEN gate, so once the decision is in, a leftover "escalated" card beside a
       running tool claims the call is still waiting on someone. */
    const atDecision = buildAgentGraph([...escalated, humanApproved]);
    assert.equal(evaluationCards(atDecision).length, 0);
    assert.equal(
      atDecision.edges.filter((e) => e.target.startsWith("approval:")).length,
      0,
      "no edge may point at a card that is no longer drawn"
    );

    // Still gone while the tool runs, and after it finishes.
    assert.equal(evaluationCards(buildAgentGraph([...escalated, humanApproved, toolRan[0]])).length, 0);
    assert.equal(evaluationCards(buildAgentGraph([...escalated, humanApproved, ...toolRan])).length, 0);

    // The gated call itself is untouched by any of this.
    const [tool] = toolCards(buildAgentGraph([...escalated, humanApproved, toolRan[0]]));
    assert.equal(tool.data.state, "running");
  });

  it("drops it on an auto verdict too, not only on a human one", () => {
    const autoApproved = [
      started,
      ...gatedCall,
      evaluationFrame("auto_approval_evaluation_started"),
      evaluationFrame("auto_approval_evaluation_ended", {
        verdict: "approve",
        reason: "routine",
        details: {}
      }),
      frame("tool_approval_resolved", {
        tool_id: "t1",
        tool_name: "refund",
        approved: true,
        reason: "routine"
      })
    ];
    assert.equal(evaluationCards(buildAgentGraph(autoApproved)).length, 0);
  });

  it("keeps a CANCELLED evaluation, which the decision caused rather than preceded", () => {
    /* The one terminal that must outlive the resolution. A human answering is what
       cancels the evaluator, so dropping the card on that same resolution would mean an
       evaluator pre-empted by a human could never be seen to have been pre-empted — and
       showing exactly that is why the cancellation is published in the first place. */
    const cancelled = [
      started,
      ...gatedCall,
      evaluationFrame("auto_approval_evaluation_started"),
      humanApproved,
      evaluationFrame("auto_approval_evaluation_superseded", { verdict: null })
    ];
    const [card] = evaluationCards(buildAgentGraph(cancelled));
    assert.equal(card.data.state, "cancelled");
    // And it stays readable while the tool it pre-empted runs.
    assert.equal(evaluationCards(buildAgentGraph([...cancelled, toolRan[0]])).length, 1);
  });

  it("lets a denied call keep its evaluation for the beat the call itself is held", () => {
    /* A denial settles the tool, and focus view holds a just-settled card one step so it is
       seen to finish. Its evaluation goes with it rather than vanishing out from under the
       card it is attached to. */
    const denied = [
      started,
      ...gatedCall,
      evaluationFrame("auto_approval_evaluation_started"),
      evaluationFrame("auto_approval_evaluation_ended", {
        verdict: "deny",
        reason: "over the limit",
        details: {}
      }),
      frame("tool_approval_resolved", {
        tool_id: "t1",
        tool_name: "refund",
        approved: false,
        reason: "over the limit"
      })
    ];
    const held = buildAgentGraph(denied, { linger: new Set(["t1"]) });
    assert.equal(evaluationCards(held).length, 1);
    assert.equal(evaluationCards(buildAgentGraph(denied)).length, 0);
  });
});

describe("gated calls made from inside a Code Mode script", () => {
  /* A Code Mode script's host calls go through the runner like any other tool, so they hit
     the same approval gate and the same evaluator. They were invisible here because a
     child tool is positioned relative to its HOST container rather than placed in the
     runtime grid, so the satellite loop — which looked the child up in the grid — found
     nothing and skipped it without a word. */
  const codeModeTurn = [
    started,
    frame("tool_requested", {
      tool_id: "cm",
      tool_name: "run_script",
      tool_input: { script: "await refund(20)" }
    }),
    frame("tool_start", { tool_id: "cm", tool_name: "run_script" }),
    // A host call the script made, gated, and judged by the evaluator.
    frame("tool_approval_requested", {
      tool_id: "t1",
      tool_name: "refund",
      tool_input: { amount: 20 }
    }),
    evaluationFrame("auto_approval_evaluation_started"),
    evaluationFrame("auto_approval_evaluation_ended", {
      verdict: "escalate",
      reason: "confidence 61% < 80%",
      details: { confidence: 0.61 }
    })
  ];

  it("draws the evaluation for a host call, wired to that call", () => {
    const graph = buildAgentGraph(codeModeTurn);

    const [card] = evaluationCards(graph);
    assert.ok(card, "a host call's approval check needs a card too");
    assert.equal(card.data.title, "jev_evaluator");
    assert.equal(card.data.state, "escalated");

    // The edge comes from the CHILD call, not from the Code Mode host: it is that call
    // being approved, and the host may have several in flight.
    const joins = graph.edges.filter((e) => e.target === card.id);
    assert.deepEqual(
      joins.map((e) => e.source),
      ["tool:t1"]
    );
    assert.ok(
      graph.nodes.some((n) => n.id === "tool:t1"),
      "the edge's source must be a node that is actually drawn"
    );
  });

  it("hangs it below the host container, clear of the children packed inside it", () => {
    const graph = buildAgentGraph(codeModeTurn);
    const host = graph.nodes.find((n) => n.id === "tool:cm");
    const child = graph.nodes.find((n) => n.id === "tool:t1");
    const [card] = evaluationCards(graph);

    /* Below the CONTAINER, not below the child cell: children are packed in a grid, so a
       satellite dropped below a child would land on the next child. */
    const hostBottom = host.position.y + host.data.nodeHeight;
    assert.ok(
      card.position.y >= hostBottom,
      `satellite at y=${card.position.y} should clear the host container ending at ${hostBottom}`
    );
    assert.ok(child.position.y < hostBottom, "the child is inside the container");
  });

  it("gives each gated host call its own column so they do not stack", () => {
    const second = (event, extra = {}) =>
      frame(event, {
        tool_id: "t2",
        tool_name: "charge",
        evaluation_id: "e2",
        evaluator: "jev_evaluator",
        ...extra
      });
    const graph = buildAgentGraph([
      ...codeModeTurn,
      frame("tool_approval_requested", {
        tool_id: "t2",
        tool_name: "charge",
        tool_input: { amount: 5 }
      }),
      second("auto_approval_evaluation_started")
    ]);

    const cards = evaluationCards(graph);
    assert.equal(cards.length, 2);
    assert.notEqual(cards[0].position.x, cards[1].position.x);
    assert.equal(cards[0].position.y, cards[1].position.y);
  });

  it("dismisses a host call's evaluation when that call's gate is decided", () => {
    const graph = buildAgentGraph([
      ...codeModeTurn,
      frame("tool_approval_resolved", {
        tool_id: "t1",
        tool_name: "refund",
        approved: true,
        reason: "fine by me"
      })
    ]);
    assert.equal(evaluationCards(graph).length, 0);
  });
});

describe("a dismissed evaluation stays dismissed", () => {
  /* The regression: an escalated check was approved by hand, the card went away, and then
     reappeared out of nowhere as the call it belonged to FINISHED — minutes later, next to
     a Code Mode host that was wrapping up.

     The cause was the linger exception keying off `lingeringTools`, which is "anything
     that settled in the last beat" and therefore includes a plain `tool_end`. An approved
     call put its own id back into that set the moment it completed, and the exception
     meant for a refusal resurrected the card. */
  const hostCallApprovedByHand = [
    started,
    frame("tool_requested", {
      tool_id: "cm",
      tool_name: "run_script",
      tool_input: { script: "await refund(20)" }
    }),
    frame("tool_start", { tool_id: "cm", tool_name: "run_script" }),
    frame("tool_approval_requested", {
      tool_id: "t1",
      tool_name: "refund",
      tool_input: { amount: 20 }
    }),
    evaluationFrame("auto_approval_evaluation_started"),
    evaluationFrame("auto_approval_evaluation_ended", {
      verdict: "escalate",
      reason: "confidence 61% < 80%",
      details: {}
    }),
    frame("tool_approval_resolved", {
      tool_id: "t1",
      tool_name: "refund",
      approved: true,
      reason: "fine by me"
    }),
    frame("tool_start", { tool_id: "t1", tool_name: "refund" })
  ];

  it("does not come back when the call it belonged to completes", () => {
    assert.equal(evaluationCards(buildAgentGraph(hostCallApprovedByHand)).length, 0);

    const finished = [
      ...hostCallApprovedByHand,
      frame("tool_end", { tool_id: "t1", tool_name: "refund", tool_output: "refunded:20" })
    ];
    /* The linger set is what the caller passes at the live edge / cursor, and a call that
       just ended is exactly what lands in it. That must not resurrect the card. */
    assert.equal(
      evaluationCards(buildAgentGraph(finished, { linger: new Set(["t1"]) })).length,
      0
    );
    assert.equal(evaluationCards(buildAgentGraph(finished)).length, 0);
  });

  it("does not come back as the Code Mode host itself finishes", () => {
    const hostDone = [
      ...hostCallApprovedByHand,
      frame("tool_end", { tool_id: "t1", tool_name: "refund", tool_output: "refunded:20" }),
      frame("tool_end", { tool_id: "cm", tool_name: "run_script", tool_output: "done" })
    ];
    assert.equal(
      evaluationCards(buildAgentGraph(hostDone, { linger: new Set(["t1", "cm"]) })).length,
      0
    );
  });

  it("still holds a REFUSED call's evaluation for the beat that call is held", () => {
    // The exception the bug came from is narrowed, not removed.
    const refused = [
      started,
      ...gatedCall,
      evaluationFrame("auto_approval_evaluation_started"),
      evaluationFrame("auto_approval_evaluation_ended", {
        verdict: "deny",
        reason: "over the limit",
        details: {}
      }),
      frame("tool_approval_resolved", {
        tool_id: "t1",
        tool_name: "refund",
        approved: false,
        reason: "over the limit"
      })
    ];
    assert.equal(
      evaluationCards(buildAgentGraph(refused, { linger: new Set(["t1"]) })).length,
      1
    );
  });
});
