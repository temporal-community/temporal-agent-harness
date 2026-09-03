// ABOUTME: Asserts the three surfaces that show an operator their own message back — chat bubble,
// replay log, session list — all show the same thing, and that the thing is the message rather than
// its envelope. renderUserMessage() existed in three copies; two checked top-level `script` but not
// `payload.script`, so a MontyDynamicAgent line wrapped as {type:"run_script", payload:{script}} and
// echoed back verbatim by agent_workflow.py's _render_message fell through to the raw value. The
// chat bubble and the replay log rendered
// {"type":"run_script","payload":{"script":"book_flight(\"SFO\", \"LHR\")"}} — escaped quotes and
// all — where the session list rendered book_flight("SFO", "LHR"). Slash commands rendered
// identically on all three, which is why it hid, so slash is the control here rather than the
// subject. Goes through the real builders, never through a copy of their logic, and ends by
// pinning that the private copies have not grown back.
//   node ui/scripts/check-user-message-rendering.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import "./libAlias.mjs";

const { buildTranscript } = await import("../src/lib/state/transcript.ts");
const { buildReplayLog } = await import("../src/lib/state/replayLog.ts");
const { renderUserMessage } = await import("../src/lib/state/inboundMessageText.ts");

/* Built the way the server builds it, not typed out as a literal: AgentMessage is
   {type, payload, expected_turn} and _render_message emits model_dump_json(include={type,payload}),
   so this is the whole of what can arrive on user_message. Writing it as JSON.stringify of that
   envelope is also what keeps the escaped-quote case honest — the script below contains the very
   quotes that made the leaked JSON unreadable. */
const wire = (type, payload) => JSON.stringify({ type, payload });

const turnStarted = (userMessage) => ({
  event: "turn_started",
  data: {
    type: "turn_started",
    agent_id: "root",
    turn_id: "turn-1",
    turn_number: 1,
    timestamp: 1,
    resume_offset: "1",
    event_offset: 1,
    user_message: userMessage
  }
});

const messageQueued = (userMessage) => ({
  event: "message_queued",
  data: {
    type: "message_queued",
    agent_id: "root",
    turn_id: "turn-2",
    turn_number: 2,
    timestamp: 2,
    resume_offset: "2",
    event_offset: 2,
    user_message: userMessage
  }
});

/* One helper per surface, each landing on the value its own component reads:
   AgentChatPanel reads TranscriptItem.text, the replay log reads ReplayLogRow.body, and the
   session list reads the string agentRun.svelte.ts stores as initial_user_message — which it
   produces by calling renderUserMessage on the same frame field. */
function chatBubble(userMessage) {
  const item = buildTranscript([turnStarted(userMessage)]).find((i) => i.kind === "user");
  assert.ok(item, "buildTranscript produced no user item for a turn_started frame");
  return item.text;
}

function replayLogBody(userMessage, frame = turnStarted) {
  const entry = { workflowId: "wf", role: "parent", label: "Agent", frame: frame(userMessage) };
  const row = buildReplayLog([entry]).rows.find((r) => r.event === entry.frame.data.type);
  assert.ok(row, `buildReplayLog produced no row for a ${entry.frame.data.type} frame`);
  return row.body;
}

const sessionList = renderUserMessage;

const onEverySurface = (userMessage) => ({
  chat: chatBubble(userMessage),
  log: replayLogBody(userMessage),
  list: sessionList(userMessage)
});

function assertAllShow(userMessage, expected, what) {
  const seen = onEverySurface(userMessage);
  for (const [surface, text] of Object.entries(seen)) {
    assert.equal(text, expected, `${what}: the ${surface} surface should show ${JSON.stringify(expected)}`);
  }
}

/* --- the bug ------------------------------------------------------------- */

const SCRIPT = 'book_flight("SFO", "LHR")';
const RUN_SCRIPT = wire("run_script", { script: SCRIPT });

assertAllShow(RUN_SCRIPT, SCRIPT, "a run_script turn");

/* Named separately from the equality above, because the failure a reader reported is not
   "the wrong string" in the abstract — it is JSON, with backslashes in it, sitting in a chat
   bubble. This is the assertion whose message says what went wrong. */
for (const [surface, text] of Object.entries(onEverySurface(RUN_SCRIPT))) {
  assert.ok(
    !text.startsWith("{"),
    `the ${surface} surface leaked the raw envelope instead of unwrapping payload.script`
  );
  assert.ok(!text.includes("\\"), `the ${surface} surface showed escaped quotes from JSON encoding`);
}

/* message_queued is replayLog.ts's second call site and renders the same field. A fix applied to
   only one of its two branches would pass everything above. */
assert.equal(
  replayLogBody(RUN_SCRIPT, messageQueued),
  SCRIPT,
  "a queued run_script message unwraps too, not just the one that started a turn"
);

/* --- the control: what already worked, and must still ---------------------- */

assertAllShow(
  wire("slash", { name: "set-model", arg: "gemini-3.5-flash" }),
  "/model gemini-3.5-flash",
  "the set-model slash command"
);
assertAllShow(wire("slash", { name: "stop" }), "/stop", "an argument-less slash command");
assertAllShow(wire("slash_command", { name: "stop" }), "/stop", "the slash_command spelling");

/* Plain prose is the common case and never JSON. It must survive untouched, including the leading
   brace that would send it down the parse path. */
assertAllShow("When should I use a local activity?", "When should I use a local activity?", "prose");
assertAllShow("{not json", "{not json", "text that only looks like an envelope");
assertAllShow(wire("chat", { text: "hello there" }), "hello there", "a wrapped chat message");

/* An envelope nothing knows how to unwrap still shows verbatim rather than blank or "undefined":
   the raw JSON is ugly but it is the whole message, and a reader can at least see it. */
const OPAQUE = wire("some_future_type", { unrecognised: 1 });
assertAllShow(OPAQUE, OPAQUE, "an unrecognised envelope");

/* The one branch the narrow copies DID have. Widening them must not have cost it. Unreachable from
   the wire — AgentMessage has no top-level `script` field — which is exactly why it could be
   dropped silently, and why it is asserted here rather than trusted. */
assertAllShow(JSON.stringify({ type: "x", script: SCRIPT }), SCRIPT, "a top-level script field");

/* payload.text wins over payload.script, in that order, because web/app.py's
   _display_user_message resolves them in that order and the two must not disagree about a message
   carrying both. */
assertAllShow(
  wire("run_script", { text: "the human sentence", script: SCRIPT }),
  "the human sentence",
  "payload.text taking precedence over payload.script"
);

/* --- one copy, not three -------------------------------------------------- */

/* This is the assertion that would have caught the bug at the time it was introduced: every
   behavioural assertion above passes on three separate implementations right up until one of them
   is edited. The divergence was the defect, not the missing branch. */
for (const path of ["../src/lib/state/transcript.ts", "../src/lib/state/replayLog.ts"]) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  assert.ok(
    source.includes('import { renderUserMessage } from "$lib/state/inboundMessageText"'),
    `${path} should import the shared renderUserMessage`
  );
  assert.ok(
    !/function renderUserMessage\b/.test(source),
    `${path} has grown its own copy of renderUserMessage again`
  );
}

console.log(
  "check-user-message-rendering: run_script unwraps on all three surfaces, slash and prose unchanged, one copy"
);
