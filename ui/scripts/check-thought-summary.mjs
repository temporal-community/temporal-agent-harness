// ABOUTME: Asserts a thought card shows the thought, whichever provider streamed it. `delta` on a
// thought_summary frame is a raw provider dump — the harness deliberately does not normalize it —
// and the UI read only Gemini's `{content:{text}}`, so every OpenAI run (`{delta}`) and every
// Pydantic AI run (`{content}` / `{content_delta}`) rendered an empty reasoning card. All shapes are
// pinned here rather than the one that was reported, because reading one shape is the defect, and
// the accumulation is pinned too: every producer streams fragments, so a projection that assigns
// instead of appending shows the tail of a thought and looks right on Gemini's single-item mock.
//   node ui/scripts/check-thought-summary.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import "./libAlias.mjs";

const { NO_THOUGHT_SUMMARY, thoughtDeltaText } = await import(
  "../src/lib/state/thoughtSummary.ts"
);
const { buildAgentGraph } = await import("../src/lib/state/flowProjection.ts");
const { buildReplayLog } = await import("../src/lib/state/replayLog.ts");
const { buildTranscript } = await import("../src/lib/state/transcript.ts");

/* The shapes, as the three producers in temporal_agent_harness/ai_sdks actually dump them —
   each copied from the payload that harness's own test asserts on. */
const SHAPES = {
  gemini: (text) => ({ type: "thought_summary", content: { type: "text", text } }),
  openai: (text) => ({
    type: "response.reasoning_summary_text.delta",
    delta: text,
    item_id: "rs_1"
  }),
  "pydantic-ai part": (text) => ({ content: text, part_kind: "thinking" }),
  "pydantic-ai delta": (text) => ({ content_delta: text, part_delta_kind: "thinking_delta" })
};

for (const [provider, shape] of Object.entries(SHAPES)) {
  assert.equal(
    thoughtDeltaText(shape("weigh the options")),
    "weigh the options",
    `${provider}: the thought text must survive extraction`
  );
}

/* A frame carrying no text at all — Pydantic AI publishes one for a signature-only delta — is
   silence, not the string "undefined", which is what a String() coercion would have produced. */
assert.equal(thoughtDeltaText({ signature_delta: "sig" }), "", "a signature delta carries no text");
assert.equal(thoughtDeltaText({}), "");
/* Gemini's content is TextContent | ImageContent, so an object without text reads as silence too. */
assert.equal(thoughtDeltaText({ type: "thought_summary", content: { type: "image" } }), "");

let offset = 0;
const meta = () => ({
  agent_id: "agent",
  turn_id: "turn-1",
  turn_number: 1,
  timestamp: 1_700_000_000 + offset,
  resume_offset: `${offset++}`
});
const frame = (event, data) => ({ event, data: { ...meta(), type: event, ...data } });

/* One turn per provider, each streaming the same thought in three fragments. */
function turn(shape, { usage } = {}) {
  offset = 0;
  return [
    frame("turn_started", { user_message: "plan something", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    ...(shape
      ? ["I should ", "check the ", "timetable first."].map((part) =>
          frame("thought_summary", { delta: shape(part) })
        )
      : []),
    frame("model_interaction_ended", { model: "gpt-5.1", usage }),
    frame("reply", { output: { text: "here you go" } }),
    frame("turn_end", {})
  ];
}

const reasoningCard = (frames) => {
  const node = buildAgentGraph(frames).nodes.find((candidate) => candidate.id === "reasoning");
  assert.ok(node, "the run should build a reasoning node");
  return node.data;
};

for (const [provider, shape] of Object.entries(SHAPES)) {
  const card = reasoningCard(turn(shape));
  assert.equal(
    card.detail,
    "I should check the timetable first.",
    `${provider}: the card must hold the whole thought, accumulated across fragments`
  );

  /* The log panel and the transcript read the same frames through their own projections, and
     each carried its own copy of the extraction that only knew Gemini. */
  const bodies = buildReplayLog(turn(shape))
    .rows.filter((row) => row.actor === "reasoning")
    .map((row) => row.body);
  assert.deepEqual(
    bodies,
    ["I should ", "check the ", "timetable first."],
    `${provider}: every thought fragment should reach the log with its text`
  );
  const thoughts = buildTranscript(turn(shape)).filter((item) => item.kind === "thought");
  assert.equal(thoughts.length, 3, `${provider}: the transcript should keep the thought fragments`);
}

/* --- a card with no text says why, rather than rendering blank -------------- */

/* Pydantic AI publishes a thought_summary for a signature-only delta, so the card is drawn and
   there is nothing to put in it. Blank is what the OpenAI bug looked like too, and one
   appearance for two states is most of the reason that bug survived. */
const silent = reasoningCard([
  frame("turn_started", { user_message: "go", turn_number: 1 }),
  frame("model_interaction_started", { model: "gpt-5.1" }),
  frame("thought_summary", { delta: { signature_delta: "sig", part_delta_kind: "thinking_delta" } }),
  frame("model_interaction_ended", { model: "gpt-5.1", usage: { thought_tokens: 512 } }),
  frame("turn_end", {})
]);
assert.equal(silent.detail, NO_THOUGHT_SUMMARY, "a thought card with no text must say why");
assert.match(NO_THOUGHT_SUMMARY, /^— /, "the unknown marker is a leading em dash, as formatCost uses");
assert.notEqual(silent.state, "idle", "and the card must not call that thinking idle");
assert.notEqual(
  reasoningCard(turn(SHAPES.openai)).detail,
  NO_THOUGHT_SUMMARY,
  "a run that did stream a summary must never claim it got none"
);

/* A run that asked for no summary at all — Reasoning(summary=None), as
   examples/sandbox_tools/coding_agent sets — streams no thought_summary frame, so it draws no
   thought card. There is no blank card to explain, and inventing one would be a card about
   nothing. */
assert.equal(
  buildAgentGraph(turn(null, { usage: { thought_tokens: 512 } })).nodes.some(
    (node) => node.id === "reasoning"
  ),
  false,
  "no thought frames, no thought card"
);

/* --- how long it thought ---------------------------------------------------- */

/* Timestamps the stream already stamps: the model call opening, and the last fragment. */
const timed = reasoningCard(turn(SHAPES.openai));
assert.equal(timed.subtitle, "Thought for 3s", `the finished card should say how long: ${timed.subtitle}`);
/* Still streaming — the end has not happened yet, so there is no duration to state. */
const midStream = buildAgentGraph([
  frame("turn_started", { user_message: "go", turn_number: 1 }),
  frame("model_interaction_started", { model: "gpt-5.1" }),
  frame("thought_summary", { delta: SHAPES.openai("thinking") })
]).nodes.find((candidate) => candidate.id === "reasoning");
assert.doesNotMatch(
  midStream.data.subtitle,
  /Thought for/,
  "a thought still arriving has no settled duration"
);

/* --- one extractor, not four ---------------------------------------------- */

/* This started as three byte-similar private copies, in the three projections asserted above,
   and the OpenAI bug was in all of them. Nothing but this stops a fourth appearing. */
for (const path of [
  "../src/lib/state/flowProjection.ts",
  "../src/lib/state/replayLog.ts",
  "../src/lib/state/transcript.ts"
]) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  assert.ok(
    source.includes('from "$lib/state/thoughtSummary"'),
    `${path} should import the shared extractor`
  );
  assert.ok(
    !/function thoughtText\b/.test(source),
    `${path} has grown its own copy of the thought extractor again`
  );
}

console.log(
  `check-thought-summary: ${Object.keys(SHAPES).length} payload shapes, fragments accumulated, ` +
    "a card with no text says why"
);
