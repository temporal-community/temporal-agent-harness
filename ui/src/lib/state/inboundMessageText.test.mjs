// ABOUTME: Asserts the three surfaces that show an operator their own message back — chat bubble,
// replay log, session list — all show the same thing, and that the thing is the message rather than
// its envelope. renderUserMessage() once existed in three copies that had drifted apart, so this
// goes through the real builders, never through a copy of their logic, and ends by pinning that the
// private copies have not grown back. The message arrives structurally now — `message_accepted`
// carries `handler` + `payload` — so there is no JSON to unwrap, but the rule for what to SHOW is
// still one rule shared by every surface: a lone string field renders bare (whatever it is named),
// and anything else is labelled by handler name. No handler or field name is special-cased.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { displayTextForMessage, renderUserMessage } from "./inboundMessageText.ts";
import { buildReplayLog } from "./replayLog.ts";
import { buildTranscript } from "./transcript.ts";

const messageAccepted = (handler, payload, disposition = "opened") => ({
  event: "message_accepted",
  data: {
    type: "message_accepted",
    agent_id: "root",
    turn_id: "turn-1",
    turn_number: 1,
    message_id: `msg-${disposition}`,
    timestamp: 1,
    resume_offset: 1,
    event_offset: 1,
    handler,
    payload,
    disposition
  }
});

/* One helper per surface, each landing on the value its own component reads:
   AgentChatPanel reads TranscriptItem.text, the replay log reads ReplayLogRow.body, and the
   session list reads the string agentRun.svelte.ts stores as initial_user_message — which it
   produces by calling renderUserMessage on the same frame fields. */
function chatBubble(handler, payload) {
  const item = buildTranscript([messageAccepted(handler, payload)]).find((i) => i.kind === "user");
  assert.ok(item, "buildTranscript produced no user item for a message_accepted frame");
  return item.text;
}

function replayLogBody(handler, payload, disposition = "opened") {
  const entry = {
    workflowId: "wf",
    role: "parent",
    label: "Agent",
    frame: messageAccepted(handler, payload, disposition)
  };
  const row = buildReplayLog([entry]).rows.find((r) => r.event === "message_accepted");
  assert.ok(row, "buildReplayLog produced no row for a message_accepted frame");
  return row.body;
}

const sessionList = renderUserMessage;

const onEverySurface = (handler, payload) => ({
  chat: chatBubble(handler, payload),
  log: replayLogBody(handler, payload),
  list: sessionList(handler, payload)
});

function assertAllShow(handler, payload, expected, what) {
  const seen = onEverySurface(handler, payload);
  for (const [surface, text] of Object.entries(seen)) {
    assert.equal(
      text,
      expected,
      `${what}: the ${surface} surface should show ${JSON.stringify(expected)}`
    );
  }
}

const SCRIPT = 'book_flight("SFO", "LHR")';

describe("the message an operator gets shown back", () => {
  it("shows a lone string field bare, whatever it is called", () => {
    assertAllShow("run_script", { script: SCRIPT }, SCRIPT, "a run_script message");
    assertAllShow("ask", { text: "hello there" }, "hello there", "a chat message");
    assertAllShow("prompt", { prompt: "plan a trip" }, "plan a trip", "a prompt field");

    /* The failure this guards is not "the wrong string" in the abstract — it is an envelope,
       with quotes escaped, sitting in a chat bubble. */
    for (const [surface, text] of Object.entries(onEverySurface("run_script", { script: SCRIPT }))) {
      assert.ok(!text.startsWith("{"), `the ${surface} surface leaked the envelope`);
      assert.ok(!text.includes("\\"), `the ${surface} surface showed escaped quotes`);
    }

    /* A queued message is replayLog.ts's other branch and renders the same fields. */
    assert.equal(
      replayLogBody("run_script", { script: SCRIPT }, "queued"),
      SCRIPT,
      "a queued run_script message renders the same as one that opened a turn"
    );
  });

  it("labels anything else by its handler, with the fields spelled out", () => {
    assertAllShow(
      "set_model",
      { model: "gemini-3.5-flash" },
      "gemini-3.5-flash",
      "an enum-constrained single field is still one string"
    );
    assertAllShow(
      "start_batch",
      { label: "nightly", size: 10 },
      'start_batch(label="nightly", size=10)',
      "a multi-field payload"
    );
    assertAllShow("stop", {}, "stop", "an empty payload is just the handler name");
    assertAllShow(
      "configure",
      { retries: 3 },
      "configure(retries=3)",
      "a lone NON-string field is labelled, since a bare number says nothing"
    );
  });

  it("shows the outbound message the same way the echoed one will render", () => {
    /* The composer's optimistic label and the frame that comes back must agree, or the
       session list flickers between two spellings of one message. */
    assert.equal(
      displayTextForMessage({ type: "ask", payload: { text: "hello there" } }),
      renderUserMessage("ask", { text: "hello there" })
    );
    assert.equal(
      displayTextForMessage({ type: "start_batch", payload: { label: "nightly", size: 10 } }),
      renderUserMessage("start_batch", { label: "nightly", size: 10 })
    );
    assert.equal(displayTextForMessage("  plain prose  "), "plain prose");
  });

  /* This is the assertion that would have caught the original bug at the time it was
     introduced: every behavioural assertion above passes on three separate implementations
     right up until one of them is edited. The divergence was the defect. */
  it("comes from one copy, not four", () => {
    for (const path of ["./transcript.ts", "./replayLog.ts", "./flowProjection.ts", "./stepTimeline.ts"]) {
      const source = readFileSync(new URL(path, import.meta.url), "utf8");
      assert.ok(
        /import \{[^}]*\brenderUserMessage\b[^}]*\} from "\$lib\/state\/inboundMessageText"/.test(
          source
        ),
        `${path} should import the shared renderUserMessage`
      );
      assert.ok(
        !/function renderUserMessage\b/.test(source),
        `${path} has grown its own copy of renderUserMessage again`
      );
    }
  });
});
