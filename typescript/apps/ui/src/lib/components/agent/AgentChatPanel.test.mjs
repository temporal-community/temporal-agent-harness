// ABOUTME: Asserts the pending-approval card says nothing about arguments a call does not take.
// A tool like start_monty is gated with tool_input={}, and the detail line rendered that as a
// literal "{}" — a line of type noise under the tool name that tells the approver nothing. The
// reverse direction is pinned too, and it is the one that is easy to break while fixing this:
// suppressing on emptiness alone (`!input`, or a general "looks empty" test) would also swallow
// UNKNOWN_TOOL_INPUT, which is the opposite of nothing to say — it means the model streamed
// arguments that were lost, and an approver deciding on a call must be told that before answering.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import AgentChatPanel, { groupKey, seedDisclosure } from "./AgentChatPanel.svelte";
import { DECLINED } from "./PendingCallbackCard.svelte";
import { jsonReplyScenarios } from "$lib/mock/jsonReplyScenarios.ts";
import { usage } from "$lib/mock/scenarios.ts";
import { scaleScenario } from "$lib/mock/scaleScenarios.ts";
import { traceSteps } from "$lib/state/replyStatus.ts";
import { UNKNOWN_TOOL_INPUT } from "$lib/state/logValue.ts";
import { buildReplayLog } from "$lib/state/replayLog.ts";
import { buildTranscript } from "$lib/state/transcript.ts";
import { highlight } from "$lib/highlight.ts";

const approvalRow = (toolId, toolName, input) => ({
  id: toolId,
  index: 0,
  ordinal: 0,
  turnNumber: 1,
  sourceTurnNumber: 1,
  turnId: "turn-1",
  timestamp: 1_700_000_000,
  event: "tool_approval_requested",
  actor: "tool",
  tone: "warn",
  label: "approval requested",
  toolId,
  toolName,
  input
});

const cardHtml = (row) =>
  render(AgentChatPanel, {
    props: {
      items: [],
      logs: [row],
      agentLabel: "Planner",
      sessionId: "wf-root"
    }
  }).body;

describe("the pending-approval card", () => {
  it("shows no detail line for a call that takes no arguments", () => {
    const body = cardHtml(approvalRow("tool-1", "start_monty", {}));

    assert.match(body, /start_monty/, "the gated call should still be named");
    assert.doesNotMatch(body, /<dl class="args/, "an empty {} rendered as an argument list");
    assert.doesNotMatch(body, /\{\}/);
  });

  it("lists the arguments a call does take by name, with no full view when nothing is hidden", () => {
    const body = cardHtml(approvalRow("tool-2", "issue_refund", { amount: 42 }));

    assert.match(body, /<dt[^>]*>amount<\/dt>\s*(?:<!--[^>]*-->)?<dd[^>]*>42<\/dd>/);
    assert.doesNotMatch(body, /Full input/);
  });

  it("offers the full input when a value is nested or too long to show", () => {
    const body = cardHtml(
      approvalRow("tool-4", "book", { trip: { city: "Palm Springs", nights: 3 }, note: "x".repeat(120) })
    );

    assert.match(body, /<details[^>]*>\s*<summary[^>]*>Full input<\/summary>/);
    const full = body.match(/<details[^>]*>[\s\S]*?<\/details>/)?.[0] ?? "";
    assert.match(full, /class="json-reply[\s\S]*?aria-label="Copy JSON"[\s\S]*?Palm Springs/, "through the shared highlighter, copyable");
  });

  it("names the card by its kind and tool, with no tool call ID line", () => {
    const body = stripComments(cardHtml(approvalRow("call_abc123", "issue_refund", { amount: 42 })));
    assert.match(body, /<article class="pending-card[^"]*" aria-label="Approval: issue_refund"/);
    assert.doesNotMatch(body, /Copy tool call ID|>tool call</, "the ID is in the trace row, not the card");
    const card = readFileSync(new URL("./PendingCard.svelte", import.meta.url), "utf8");
    assert.doesNotMatch(card, /\.ids\b|<dl/, "no card-local ID-list styles");
  });

  it("shows a nested script whole, line by line, under its key path", () => {
    const script = "import asyncio\n\nasync def main():\n    board = await read_trip_board()\n    return board\n\nasyncio.run(main())";
    const body = cardHtml(
      approvalRow("call_235193", "monty_run_script", { subagent: "d69656-f486d1", message: { script } })
    );

    assert.match(body, /<dt[^>]*>message\.script<\/dt>\s*(?:<!--[^>]*-->)?<dd class="[^"]*\bblock\b[^"]*"><div class="markdown-message compact[\s\S]*?<span class="code-language-tag">python<\/span>/);
    for (const word of ["read_trip_board", "return", "asyncio", "run", "main"]) assert.ok(body.includes(word), word);
    assert.doesNotMatch(body, /import async\.\.\./, "the approver must see the script, not its log preview");
    assert.doesNotMatch(body, /Full input/, "nothing is hidden, so there is no fuller view to offer");
  });

  it("still says so when the arguments are unknown", () => {
    const body = cardHtml(approvalRow("tool-3", "issue_refund", null));

    assert.match(body, /class="prose unknown/);
    assert.ok(
      body.includes("stream ended before they could be parsed"),
      "lost arguments must be reported, not suppressed as if the call took none"
    );
    assert.ok(UNKNOWN_TOOL_INPUT.startsWith("—"), "the unknown marker is a leading em dash");
  });
});

/* The ID shown beside a copy button is the one it copies: both sit in one `.copyable`. */
describe("chat copy controls", () => {
  it("offers the error message a failed session shows", () => {
    const body = stripComments(
      render(AgentChatPanel, {
        props: { items: [], logs: [], agentLabel: "Planner", sessionId: "wf-root", error: "Worker lost: boom" }
      }).body
    );
    assert.match(
      body,
      /class="copyable[^"]*">\s*<span[^>]*>Worker lost: boom<\/span>\s*<button[^>]*aria-label="Copy error"/
    );
  });

  it("offers each drawn message whole, the reader's and the agent's, inside the windowed list", () => {
    const items = buildTranscript(scaleScenario("transcript5k").frames);
    const body = stripComments(
      render(AgentChatPanel, { props: { items, logs: [], agentLabel: "Planner", sessionId: "wf-root" } }).body
    );
    const articles = body.split(/<article class="message /).slice(1);
    assert.ok(articles.some((a) => a.startsWith("user")) && articles.some((a) => a.startsWith("assistant")));
    for (const article of articles) {
      assert.match(article, /class="message-copy[^"]*"><span class="copyable[^"]*">\s*<button[^>]*aria-label="Copy message"/);
    }
  });
});

const user = (turnNumber, text = "Why is the export slow?") => ({ kind: "user", id: `u-${turnNumber}`, turnNumber, text, timestamp: 100 });
const reply = (turnNumber, text, extra = {}) => ({
  kind: "agent", id: `a-${turnNumber}`, turnNumber, text, streaming: false, timestamp: 101, citations: [], data: false, ...extra
});
const row = (event, timestamp, data = {}) => ({
  event,
  data: { type: event, agent_id: "a", turn_number: 1, turn_id: "turn-1", timestamp, ...data }
});
const chat = (props) =>
  stripComments(render(AgentChatPanel, { props: { logs: [], agentLabel: "Planner", sessionId: "wf-root", ...props } }).body);
const articles = (body) => body.split(/<article class="message /).slice(1);

describe("the multi-line composer", () => {
  const handler = (name, field, description) => ({
    name,
    description,
    parameters: {
      type: "object",
      properties: { [field]: { type: "string", title: field[0].toUpperCase() + field.slice(1) } },
      required: [field]
    },
    output: {},
    mid_turn: "enqueue",
    model_callable: false
  });

  it("defaults mixed prose and script handlers to the prose composer", () => {
    const body = chat({
      items: [],
      agentInterface: [
        handler("execute", "script", "Run a flow script."),
        handler("generate", "text", "Describe the flow you want.")
      ]
    });
    assert.match(body, /<textarea[^>]*rows="1"[^>]*placeholder="Message"/);
    assert.match(body, /aria-label="Handler: generate"/);
    assert.match(
      body,
      /<form class="composer[^"]*"[\s\S]*?<textarea[\s\S]*?<div class="composer-controls[^"]*"[\s\S]*?aria-label="Handler: generate"/,
      "the handler picker belongs inside the composer"
    );
  });

  it("keeps the picker quiet and makes send the one filled control", () => {
    const body = chat({
      items: [],
      agentInterface: [
        handler("execute", "script", "Run a flow script."),
        handler("generate", "text", "Describe the flow you want.")
      ]
    });
    const iconButton = readFileSync(new URL("../primitives/IconButton.svelte", import.meta.url), "utf8");
    assert.match(body, /class="icon-button default choice-button handler-button[^"]*"/);
    assert.match(body, /class="icon-button accent[^"]*"[^>]*aria-label="Send message"/);
    assert.match(iconButton, /\.icon-button\.accent \{[\s\S]*?background: var\(--accent\);/);
    assert.match(iconButton, /\.icon-button\.accent:disabled,[\s\S]*?background: var\(--surface-3\);/);
  });

  it("opens and toggles the handler picker without changing the draft", () => {
    const body = chat({
      items: [],
      agentInterface: [
        handler("ask", "text", "Ask a question."),
        handler("generate", "text", "Write prose.")
      ]
    });
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    const toggle = source.slice(source.indexOf("function togglePicker"), source.indexOf("function schemaRecord"));
    const button = source.match(/<IconButton\s+class=\{`choice-button handler-button[\s\S]*?<\/IconButton>/)[0];

    assert.match(body, /aria-haspopup="listbox" aria-expanded="false"/);
    assert.match(body, /data-tip="Handler: ask\. Type \/ to choose\."/);
    assert.match(button, /onclick=\{togglePicker\}/);
    assert.doesNotMatch(button, /draft\s*=/, "the picker control must not manufacture a slash draft");
    assert.match(toggle, /if \(pickerOpen\) return closePicker\(\);[\s\S]*pickerButtonOpen = true;/, "a second click closes the same picker");
  });

  it("keeps chip-open drafts when choosing a handler and retains the typed slash shortcut", () => {
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    const select = source.slice(source.indexOf("function selectHandler"), source.indexOf("function schemaRecord"));

    assert.match(select, /const keepDraft = pickerButtonOpen;[\s\S]*if \(!keepDraft\) draft = "";/);
    assert.match(source, /draft\.trimStart\(\)\.startsWith\("\/"\) && dismissedPickerDraft !== draft/);
    assert.match(source, /function handleComposerInput\(\): void \{\s*dismissedPickerDraft = null;/);
  });

  it("keeps prose compact and expands script mode", () => {
    const prose = chat({ items: [], agentInterface: [handler("generate", "text", "Write prose.")] });
    const script = chat({ items: [], agentInterface: [handler("execute", "script", "Run Python.")] });
    assert.match(prose, /<form class="composer(?:\s|")/);
    assert.doesNotMatch(prose, /<form class="composer[^"]*\bexpanded\b/);
    assert.match(script, /<form class="composer expanded code-input[^"]*"/);
    assert.match(script, /class="visually-hidden">Runs this Python as written\.<\/span>/);
    assert.doesNotMatch(script, /class="composer-hint"/);
  });

  it("uses a fixed placeholder and centers the compact row", () => {
    const body = chat({
      items: [],
      agentInterface: [{
        ...handler("ask", "text", "Ask."),
        parameters: {
          type: "object",
          properties: { text: { type: "string", title: "text" } },
          required: ["text"]
        }
      }]
    });
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    assert.match(body, /placeholder="Message"/);
    assert.match(body, /aria-label="Message Planner"/);
    assert.match(source, /\.composer \{[\s\S]*?align-items: center;[\s\S]*?padding: var\(--gutter-tight\);/);
    assert.match(source, /\.composer:not\(\.expanded\) textarea \{[\s\S]*?height: var\(--control-height\);[\s\S]*?padding-block:/);
  });

  it("keeps the handler picker within the composer when opened from its chip", () => {
    // Clicking the chip (`chip-open`) used to right-anchor the picker to the chip's own edge
    // and give it a fixed width, which could push its left edge past the composer's — the chip
    // sits mid-row, with the send button and spacer still to its right. The picker's left/right
    // must keep coming from the base rule (the composer's own edges) in every open state; only
    // the vertical offset should track the chip, so a tall, expanded composer still opens the
    // picker right above its row.
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    const base = source.match(/\.handler-picker \{([\s\S]*?)\n  \}/)?.[1] ?? "";
    const chipOpen = source.match(/\.handler-picker\.chip-open \{([\s\S]*?)\n    \}/)?.[1] ?? "";
    assert.match(base, /right: 0;/);
    assert.match(base, /left: 0;/);
    assert.match(chipOpen, /position-anchor: --handler-picker;/);
    assert.match(chipOpen, /bottom: calc\(anchor\(top\) \+ 8px\);/);
    assert.doesNotMatch(chipOpen, /right:|left:|width:/);
  });

  it("leaves the textarea box to the composer and caps auto-growth", () => {
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    const textarea = source.match(/\.composer textarea \{([\s\S]*?)\n  \}/)?.[1] ?? "";
    const script = source.match(/\.composer\.code-input \{([\s\S]*?)\n  \}/)?.[1] ?? "";
    assert.match(textarea, /field-sizing: content;/);
    assert.match(textarea, /min-height: var\(--control-height\);/);
    assert.match(textarea, /max-height: min\(var\(--composer-max-height\), 35vh\);/);
    assert.match(textarea, /resize: none;/);
    assert.match(textarea, /border: 0;/);
    assert.match(textarea, /border-radius: 0;/);
    assert.match(textarea, /background: none;/);
    assert.match(textarea, /box-shadow: none;/);
    assert.match(script, /--composer-max-height: 23\.2em;/);
    assert.doesNotMatch(script, /min-height:/);
    const focus = source.match(/\.composer:focus-within \{([\s\S]*?)\n  \}/)?.[1] ?? "";
    assert.match(focus, /outline: 2px solid var\(--focus-ring\);/);
    assert.match(focus, /outline-offset: -1px;/);
    assert.doesNotMatch(focus, /box-shadow/, "the composer has the shared field focus treatment once");
  });

  it("dissolves the sent draft only after a successful send", () => {
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    const send = source.slice(source.indexOf("async function sendMessage"), source.indexOf("/** Send the schema-driven form"));

    assert.match(source, /<textarea\s+use:inputClearDissolve/);
    assert.match(send, /const sent = await dispatchMessage[\s\S]*if \(sent\) \{[\s\S]*dissolveClear\(composerInput, \(\) => \(draft = ""\)\)/);
    assert.doesNotMatch(send.slice(0, send.indexOf("if (sent)")), /dissolveClear/, "a failed send keeps the draft without dissolving it");
  });
});

describe("composer recall", () => {
  it("recalls only the latest sent message from an empty draft and Escape restores it", () => {
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    assert.match(source, /if \(draft !== "" \|\| historyIndex !== -1\) return false;/);
    assert.match(source, /const latest = sentUserMessages\.at\(-1\);/);
    assert.match(source, /if \(event\.key === "Escape" && restoreRecalledDraft\(\)\)/);
    assert.ok(
      source.indexOf("movePickerSelection(-1)") < source.indexOf('event.key === "ArrowUp" && recallLastMessage()'),
      "the slash picker consumes ArrowUp before recall"
    );
  });
});

describe("server-owned queued messages", () => {
  const queuedFrames = [
    row("message_accepted", 100, {
      disposition: "queued",
      handler: "ask",
      message_id: "message-queued",
      payload: { text: "follow up after this reply\nwith more detail" }
    })
  ];

  it("previews admitted queued messages without edit or remove controls", () => {
    const body = chat({ items: [], logs: buildReplayLog(queuedFrames).rows });
    assert.match(body, /aria-label="Queued messages"/);
    assert.match(body, />1 waiting<\/span>[\s\S]*?<span[^>]*>·<\/span>[\s\S]*?Follow up after this reply/);
    assert.match(body, /data-tip="follow up after this reply\s+with more detail"/);
    assert.match(body, /Follow up after this reply/);
    const hint = body.match(/<p class="composer-hint-slot[\s\S]*?<\/p>/)?.[0] ?? "";
    assert.doesNotMatch(hint, /class="chip|<button|<a\b/);
    assert.doesNotMatch(body, /Edit queued|Remove queued|Stop and send/);
  });

  it("summarizes multiple queued messages without boxing their contents", () => {
    const body = chat({
      items: [],
      logs: buildReplayLog([
        ...queuedFrames,
        row("message_accepted", 101, {
          turn_number: 2,
          disposition: "queued",
          handler: "ask",
          message_id: "message-queued-2",
          payload: { text: "Second queued message" }
        })
      ]).rows
    });
    assert.match(body, />2 waiting<\/span>/);
    assert.match(body, /data-tip="[^"]*follow up[^"]*Second queued message"/);
    assert.doesNotMatch(body, />\s*Second queued message\s*</);
  });

  it("removes the preview once that queued turn starts", () => {
    const body = chat({
      items: [],
      logs: buildReplayLog([...queuedFrames, row("turn_started", 101)]).rows
    });
    assert.doesNotMatch(body, /aria-label="Queued messages"/);
  });
});

describe("schema-gated attachments", () => {
  const attachmentHandler = (attachmentSchema) => ({
    name: "ask_with_files",
    description: "Ask with supporting files.",
    parameters: {
      type: "object",
      properties: {
        text: { type: "string", title: "Text" },
        attachments: attachmentSchema
      },
      required: ["text"]
    },
    output: {},
    mid_turn: "enqueue",
    model_callable: false
  });

  it("renders no attachment control for a text-only handler", () => {
    const body = chat({
      items: [],
      agentInterface: [{
        name: "ask",
        description: "Ask.",
        parameters: {
          type: "object",
          properties: { text: { type: "string" } },
          required: ["text"]
        },
        output: {},
        mid_turn: "enqueue",
        model_callable: false
      }]
    });
    assert.doesNotMatch(body, /aria-label="Add attachments"|type="file"/);
  });

  it("shows the attachment control for named and annotated file schemas", () => {
    for (const schema of [
      { type: "array", items: { type: "string", contentEncoding: "base64" } },
      { type: "string", contentMediaType: "image/png", contentEncoding: "base64" },
      { type: "string", format: "binary" },
      { type: "string", format: "uri" },
      {
        type: "array",
        items: {
          type: "object",
          properties: { type: { enum: ["image", "file"] }, data: { type: "string" } }
        }
      }
    ]) {
      const body = chat({ items: [], agentInterface: [attachmentHandler(schema)] });
      assert.match(body, /aria-label="Add attachments"/);
      assert.match(body, /type="file"/);
    }
  });
});

/* The turn header and a placeholder reply under it said the same thing twice, a row apart. */
describe("a turn in flight", () => {
  it("shows its status in its header, with no placeholder reply", () => {
    const body = chat({ items: [user(1)], logs: buildReplayLog([row("turn_started", 100), row("model_interaction_started", 100)]).rows });
    assert.match(body, /<button[^>]*class="activity-summary[^"]*turn-summary[^"]*"[\s\S]*?class="reply-status[\s\S]*?Thinking/);
    assert.doesNotMatch(body, /reply-pending|class="message assistant/);
  });

  it("still shows it before the turn has published a step", () => {
    const body = chat({ items: [user(1)], logs: buildReplayLog([row("turn_started", 100)]).rows });
    assert.match(body, /<div class="activity-summary[^"]*turn-summary[^"]*">[\s\S]*?class="reply-status[\s\S]*?Thinking/);
    assert.doesNotMatch(body, /aria-expanded/, "nothing to open, so no toggle");
  });

  it("keeps one header height, working or settled", () => {
    const source = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
    assert.match(source("./AgentChatPanel.svelte"), /\.activity-summary \{\s*height: var\(--control-height-xs\);/);
    assert.match(source("./ReplyStatus.svelte"), /\.reply-status \{\s*height: var\(--control-height-xs\);/);
  });
});

/* A script-mode message was flattened to bubble text and drawn as markdown, so its indented
   lines became paragraphs and code blocks. It is code: drawn verbatim, never as markup. */
describe("a script message", () => {
  const sent = (turn, payload) =>
    row("message_accepted", 100 + turn, { turn_number: turn, turn_id: `turn-${turn}`, message_id: `m-${turn}`, handler: "run", payload });
  const bubbleClasses = (article) => article.match(/<div class="bubble([^"]*)"/)?.[1].split(/\s+/) ?? [];
  const codeIn = (article) => article.match(/<pre class="md-code-block[^"]*"[^>]*><code[^>]*>([\s\S]*?)<\/code><\/pre>/)?.[1];
  const scripts = {
    indented: "def main():\n    for x in range(3):\n\tif x:\n\t    print(x)  \n\n    return 1",
    long: `x = "${"a".repeat(2_000)}"`,
    empty: "",
    markup: '<script>alert(1)</script>\n# heading\n**bold** <b>b</b> [link](javascript:alert(1))'
  };
  const entries = Object.entries(scripts);
  const body = chat({
    items: buildTranscript([
      ...entries.map(([, script], index) => sent(index + 1, { script })),
      sent(entries.length + 1, { text: "It **works**" })
    ])
  });
  const users = articles(body).filter((article) => article.startsWith("user"));

  it("draws each script verbatim inside pre and code, with its indentation intact", () => {
    assert.equal(users.length, entries.length + 1, users.length);
    assert.match(codeIn(users[0]), /\n {4}<span[^>]*>for<\/span> x[\s\S]*\n\t<span[^>]*>if<\/span> x:\n\t {4}print/);
    entries.forEach(([name, script], index) => {
      assert.equal(codeIn(users[index]), highlight(script, "python"), `${name}: drawn verbatim by the shared highlighter`);
      assert.ok(bubbleClasses(users[index]).includes("script"), name);
      assert.doesNotMatch(users[index], /aria-label="Copy message"/, `${name}: the block's own copy is enough`);
    });
  });

  it("never renders a script's markup-looking text as markup", () => {
    const markup = users[entries.findIndex(([name]) => name === "markup")];
    assert.doesNotMatch(markup, /<script>|<h\d|<strong>|<b>|<a\b/i);
    assert.match(markup, /&lt;script&gt;alert/);
  });

  it("keeps a long line on one scrolling line instead of wrapping it", () => {
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    assert.match(source, /\.bubble\.script :global\(pre code\) \{\s*white-space: pre;/);
  });

  it("still draws a text message as markdown", () => {
    const text = users.at(-1);
    assert.match(text, /<strong>works<\/strong>/);
    assert.ok(!bubbleClasses(text).includes("script"));
    assert.doesNotMatch(text, /md-code-wrap/);
  });
});

describe("an assistant reply", () => {
  it("has no avatar: the bubble starts the message", () => {
    const body = chat({ items: [user(1), reply(1, "It retries every 30 minutes.")] });
    const [, assistant] = articles(body);
    assert.match(assistant, /^assistant[^"]*">\s*<div class="bubble/);
    assert.doesNotMatch(body, /assistant-avatar/);
  });

  it("offers a copy of itself in the bubble's own corner", () => {
    const [, assistant] = articles(chat({ items: [user(1), reply(1, "It retries **every** 30 minutes.")] }));
    assert.match(assistant, /<div class="bubble[^"]*has-copy[^"]*">[\s\S]*?class="message-copy[^"]*"><span class="copyable[^"]*">\s*<button[^>]*aria-label="Copy message"/);
  });

  it("leaves a lone JSON or code block to the block's own copy", () => {
    const json = { records: [] };
    const data = articles(chat({ items: [user(1), reply(1, JSON.stringify(json), { data: true, json })] }))[1];
    const code = articles(chat({ items: [user(1), reply(1, "```py\nprint(1)\n```")] }))[1];
    for (const article of [data, code]) {
      assert.doesNotMatch(article, /Copy message|has-copy/);
    }
    assert.match(code, /aria-label="Copy code"/);
  });

  it("keeps it on a JSON reply that failed before it parsed, which has no copy of its own", () => {
    const failed = articles(chat({ items: [user(1), reply(1, '{"records": [', { data: true, error: "ValidationError" })] }))[1];
    assert.doesNotMatch(failed, /Copy JSON/);
    assert.match(failed, /aria-label="Copy message"/);
    /* A data reply's own `padding: 0` once took the copy's room, and it sat on the error. Every
       reply is frameless now, so nothing overrides the room. */
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    assert.doesNotMatch(source, /\.bubble\.data[^{]*\{/);
    assert.match(source, /\n {2}\.bubble\.has-copy \{[^}]*padding-bottom: var\(--control-height-xs\);/);
  });
});

/* Keyed by message alone, so a message that did not change keeps its DOM. The key used to
   carry the list index, because ids were per turn and a shared turn holds several messages. */
describe("chat message keys", () => {
  it("are the transcript's per-message ids, unique even when a turn holds several", () => {
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    assert.match(source, /\{#each \$messageRows\.getVirtualItems\(\) as row \(row\.key\)\}/);
    assert.match(source, /getItemKey: \(index\) => messages\[index\]\?\.id/);
    assert.equal(source.match(/id: `chat-\$\{item\.id\}`/g)?.length, 2);
    const items = buildTranscript(jsonReplyScenarios().worst.frames).filter((item) => item.kind === "user" || item.kind === "agent");
    assert.ok(items.filter((item) => item.turnNumber === 10).length > 2, "sanity: turn 10 holds several messages");
    assert.equal(new Set(items.map((item) => item.id)).size, items.length);
  });
});

/* Every change used to set `scrollTop = scrollHeight`, so a reader scrolled up mid-reply was
   back at the end within 300ms. The tail is followed only while the reader is at it. */
describe("following the tail", () => {
  it("never scrolls a reader who has scrolled up, and offers the way back", () => {
    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    assert.doesNotMatch(source, /scrollTop\s*=\s*[^;]*scrollHeight/);
    assert.match(source, /anchorTo: "end",\s*followOnAppend: true,\s*scrollEndThreshold: FOLLOW_SLACK/);
    assert.match(
      source,
      /chatScrollSignature;\s*if \(!untrack\(\(\) => following\)\) \{\s*unseen = true;\s*return;\s*\}\s*void tick\(\)\.then\(\(\) => messageList\(\)\.scrollToEnd\(\)\);/
    );
    assert.match(source, /onscroll=\{trackFollowing\}/);
    assert.match(source, /\{#if !following && unseen\}\s*<Chip class="jump-latest"[^>]*onclick=\{jumpToLatest\}/);
  });
});

describe("a long conversation", () => {
  it("draws a screenful of 5,000 messages, not all of them", () => {
    const items = buildTranscript(scaleScenario("transcript5k").frames);
    const body = render(AgentChatPanel, { props: { items, logs: [], agentLabel: "Planner", sessionId: "wf-root" } }).body;
    const drawn = body.match(/<article class="message /g)?.length ?? 0;
    assert.ok(drawn > 0 && drawn < 30, `drew ${drawn} messages`);
  });
});

/* A provider that reports no usage left the turn header saying "total 0 tok" while the Tokens
   pane, reading the same calls, said "Unknown, not zero". */
describe("a turn's token total", () => {
  const turn = (usage) => {
    const frames = [
      row("turn_started", 100, { user_message: "Weather?" }),
      row("model_interaction_started", 100, { model: "gpt-5.1" }),
      row("model_interaction_ended", 101, { model: "gpt-5.1", usage }),
      row("message_handler_end", 102, { output: { text: "Sunny." } }),
      row("turn_end", 102)
    ];
    const body = chat({ items: [user(1), reply(1, "Sunny.")], logs: buildReplayLog(frames).rows });
    return body.match(/class="turn-tokens[^"]*">([^<]*)</)?.[1];
  };

  it("is unknown, not zero, when the model reported no usage", () => {
    assert.equal(turn(null), "total — tok");
    assert.equal(turn(usage(40, 2, 0, 0)), "total 42 tok");
  });

  /* While working, the live header read "Thinking 1.4s · total 0 tok": the call in flight had
     not reported, yet the sum of none was printed as a figure. */
  it("is unknown, not zero, while the turn works", () => {
    const working = (frames) =>
      chat({ items: [user(1)], logs: buildReplayLog(frames).rows }).match(/class="turn-tokens[^"]*">([^<]*)</)?.[1];
    const started = [row("turn_started", 100, { user_message: "Weather?" }), row("model_interaction_started", 100, { model: "gpt-5.1" })];
    assert.equal(working(started), "total — tok");
    assert.equal(
      working([
        ...started,
        row("model_interaction_ended", 101, { model: "gpt-5.1", usage: usage(40, 2, 0, 0) }),
        row("model_interaction_started", 102, { model: "gpt-5.1" })
      ]),
      "total ≥ 42 tok"
    );
  });
});

/* A turn whose only gated call was rejected read "Replied · 17s"; a declined question read the same. */
describe("a turn that refused a gated call", () => {
  it("says so in its summary: the reject glyph and a count, the word for screen readers and the tip", () => {
    const frames = [
      row("turn_started", 100, { user_message: "Book it" }),
      row("tool_approval_requested", 101, { tool_id: "t1", tool_name: "book_flight", tool_input: {} }),
      row("tool_approval_resolved", 105, { tool_id: "t1", tool_name: "book_flight", approved: false, reason: "Not now" }),
      row("callback_requested", 106, { tool_id: "c1", tool_name: "ask_user", tool_input: {}, output_schema: {} }),
      row("callback_resolved", 110, { tool_id: "c1", tool_name: "ask_user", outcome: "error", error: DECLINED }),
      row("callback_requested", 111, { tool_id: "c2", tool_name: "edit_code", tool_input: {}, output_schema: {} }),
      row("callback_resolved", 112, { tool_id: "c2", tool_name: "edit_code", outcome: "error", error: "old_text not found" }),
      row("message_handler_end", 117, { output: { text: "Okay." } }),
      row("turn_end", 117)
    ];
    const body = chat({ items: [user(1), reply(1, "Okay.")], logs: buildReplayLog(frames).rows });
    const heading = body.match(/<span class="activity-heading[\s\S]*?<span class="turn-tokens/)?.[0] ?? "";
    assert.match(heading, /<strong[^>]*>Replied · 17s<\/strong>/);
    const refusals = [...heading.matchAll(/<span class="turn-refusal[^"]*" data-tip="([^"]+)">\s*<span class="state-glyph[\s\S]*?lucide-circle-x[\s\S]*?<\/span>(\d+)<span class="visually-hidden"> ([a-z]+) <\/span>/g)];
    assert.deepEqual(refusals.map((m) => [m[1], m[2], m[3]]), [["1 rejected", "1", "rejected"], ["1 declined", "1", "declined"]], "a client tool's failure is not a decline");
  });
});

/* The trace drew a row per raw event, each with the same clock time, a boxed "host call" badge
   with a wrench, and the call's arguments cut off mid-token. A Jev auto-mode travel turn —
   gated scripts, gated host calls, a state change — now reads as one row per step. */
describe("a turn's trace", () => {
  const { frames } = scaleScenario("reply-travel");
  const body = chat({ items: buildTranscript(frames), logs: buildReplayLog(frames).rows });
  const trace = body.match(/<div class="activity-trace[\s\S]*$/)?.[0] ?? "";
  const rows = trace.match(/<(button|div)[^>]*trace-line[\s\S]*?<\/\1>/g) ?? [];
  const text = (html) => [...html.matchAll(/>([^<]*)</g)].map((match) => match[1]).join(" ").replace(/\s+/g, " ").trim();

  it("draws one row per step", () => {
    assert.equal(rows.length, 9, rows.map(text).join("\n"));
    assert.match(text(rows[1]), /^run_travel_script approved by jev 900ms$/);
    assert.match(text(rows[2]), /^host read_trip_board 50ms$/);
  });

  /* Every row drew a chevron, nested ones too, so the trace read as a column of toggles. A row
     only opens when there is more to see than its line, and says so on hover or focus. */
  it("makes a toggle only of a row with something to open", () => {
    const toggles = rows.filter((row) => row.startsWith("<button"));
    const plain = rows.filter((row) => row.startsWith("<div"));
    assert.deepEqual(plain.map(text), ["gemini-3.8-flash 2,700 tok 4.6s", "gemini-3.8-flash 3,690 tok 7.5s"]);
    assert.equal(toggles.length, 7);
    for (const row of toggles) {
      assert.match(row, /^<button[^>]*class="[^"]*\bdisclosure\b[^"]*"[^>]*aria-expanded="false"/);
      assert.match(row, /<svg[^>]*class="[^"]*\bdisclosure-chevron\b/, "the caret the hover reveals");
    }
    for (const row of plain) {
      assert.doesNotMatch(row, /aria-expanded|role="button"|disclosure|<svg[^>]*chevron/);
    }
  });

  /* Every step read at one weight, and a settled turn folded its failures away under "Ran 5
     tools", so the reader saw the reply and never the error behind it. */
  it("dims finished steps and keeps failed, rejected or waiting ones open at full contrast", () => {
    const settled = (name) => {
      const { frames } = scaleScenario(name);
      return chat({ items: buildTranscript(frames), logs: buildReplayLog(frames).rows });
    };
    const failed = settled("reply-failed");
    assert.match(failed, /<button[^>]*class="activity-summary[^"]*turn-summary[^"]*"[^>]*aria-expanded="true"/);
    const glyphs = [...failed.matchAll(/class="trace-row[^"]*glyph-(\w+)/g)].map((match) => match[1]);
    assert.ok(glyphs.includes("complete") && glyphs.filter((glyph) => glyph === "error").length === 3, glyphs.join(" "));
    assert.doesNotMatch(settled("reply-tools50"), /aria-expanded="true"/, "a routine settled turn still folds");

    const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
    const color = (selector) => source.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} \\{\\s*color: var\\((--text-\\d)\\)`))?.[1];
    assert.equal(color(".activity-list :global(.trace-row.glyph-complete > .trace-line strong)"), "--text-3");
    assert.equal(color(".activity-list :global(.trace-row:is(.glyph-error, .glyph-approval) > .trace-line :is(strong, .trace-note))"), "--text-1");
    assert.match(source, /\.bubble \{[^}]*color: var\(--text-1\);/, "the agent's words stay full contrast");
  });

  it("puts no badge, wrench, highlight box, clock time or payload on a row", () => {
    for (const row of rows) {
      assert.doesNotMatch(row, /class="chip|lucide-wrench|highlight-/);
      assert.doesNotMatch(text(row), /\d:\d\d|[{}[\]"]/);
    }
    assert.ok(rows.every((row) => /data-tip="[^"]*\d:\d\d/.test(row)), "the time is in the row's tip");
  });
});

/* A turn number is not a feed. One turn holds several messages, and continue-as-new
   reuses the numbers, so opening one message's trace used to open every other feed
   on that number — and a nested group, keyed only by its step, opened in each of them. */
describe("activity feed disclosures", () => {
  const HIDDEN = [
    "message_accepted",
    "message_handler_start",
    "message_handler_end",
    "turn_started",
    "turn_end",
    "reply_delta",
    "text_annotation"
  ];
  const frame = (event, turn, messageId, timestamp, data = {}) => ({
    event,
    data: {
      type: event,
      agent_id: "7f3c1a",
      turn_number: turn,
      turn_id: `turn-${turn}`,
      message_id: messageId,
      timestamp,
      ...data
    }
  });
  const feedId = (messageId) => `chat-user-${messageId}`;

  function turnFrames(turn, messageIds, t0) {
    const id = messageIds[0];
    const script = { tool_id: `s-${turn}`, tool_name: "run_travel_code", tool_input: { script: "board = open_trip()\n" } };
    const host = { tool_id: `h-${turn}`, tool_name: "open_trip", tool_input: { city: "Paris" } };
    return [
      ...messageIds.map((messageId) =>
        frame("message_accepted", turn, messageId, t0, {
          handler: "ask",
          payload: { text: `Plan ${messageId}` },
          disposition: "opened"
        })
      ),
      frame("turn_started", turn, id, t0),
      frame("model_interaction_started", turn, id, t0 + 1, { model: "gemini-3.8-flash" }),
      frame("thought_summary", turn, id, t0 + 2, { delta: { content: { text: "Check the board first." } } }),
      frame("tool_requested", turn, id, t0 + 3, script),
      frame("model_interaction_ended", turn, id, t0 + 4, { model: "gemini-3.8-flash", usage: usage(10, 2, 0, 0) }),
      frame("tool_start", turn, id, t0 + 5, script),
      frame("tool_start", turn, id, t0 + 6, host),
      frame("state_patch", turn, id, t0 + 7, {
        state_id: "trip_board",
        version: 1,
        ops: [{ op: "add", path: "/trips/-", value: { city: "Paris" } }]
      }),
      frame("tool_end", turn, id, t0 + 8, { ...host, tool_output: "ok" }),
      frame("tool_end", turn, id, t0 + 9, { ...script, tool_output: "ok" }),
      frame("reply_delta", turn, id, t0 + 10, { text: "Nothing booked yet." }),
      frame("message_handler_end", turn, id, t0 + 11, { output: { text: "Nothing booked yet." } }),
      frame("turn_end", turn, id, t0 + 12)
    ];
  }

  const frames = [...turnFrames(1, ["msg-1a", "msg-1b"], 100), ...turnFrames(2, ["msg-2"], 200)];
  const items = buildTranscript(frames);
  const logs = buildReplayLog(frames).rows;
  const visible = logs.filter(
    (row) => row.turnNumber > 0 && row.actor !== "user" && !HIDDEN.includes(row.event)
  );
  const stateStep = traceSteps(visible.filter((row) => row.turnNumber === 1)).find((step) => step.name === "State changed");
  const feedA = feedId("msg-1a");
  const feedB = feedId("msg-1b");

  function rendered(seed) {
    seedDisclosure(seed);
    try {
      return chat({ items, logs });
    } finally {
      seedDisclosure(null);
    }
  }

  function feedsOf(body) {
    const found = new Map();
    for (const row of body.split(/<div class="message-row/).slice(1)) {
      const text = row.match(/Plan msg-[^<]+/)?.[0];
      if (!text) continue;
      found.set(text, row.split(/<div class="activity-feed/).slice(1)[0] ?? "");
    }
    return found;
  }

  const buttonFor = (feed, name) =>
    (feed.match(/<button\b[\s\S]*?<\/button>/g) ?? []).find((button) => button.includes(name));

  it("opens one feed and leaves the other turn, the other message, and nested groups shut", () => {
    const feeds = feedsOf(rendered({ feeds: { [feedA]: true }, groups: [], thoughts: [] }));
    const a = feeds.get("Plan msg-1a");
    const b = feeds.get("Plan msg-1b");
    const c = feeds.get("Plan msg-2");
    assert.ok(a && b && c, [...feeds.keys()].join(", "));
    assert.ok(a.startsWith(" expanded"), "the feed that was opened");
    assert.ok(!b.startsWith(" expanded"), "the other message on the same turn stays shut");
    assert.ok(!c.startsWith(" expanded"), "the other turn stays shut");
    assert.match(a, /class="activity-trace/);
    assert.doesNotMatch(b, /class="activity-trace/);
    assert.doesNotMatch(c, /class="activity-trace/);
    for (const name of ["run_travel_code", "State changed", "open_trip"]) {
      assert.match(buttonFor(a, name), /aria-expanded="false"/, name);
    }
    const thought = a.match(/<details\b[^>]*>/)?.[0] ?? "";
    assert.match(thought, /thought/);
    assert.doesNotMatch(thought, /\bopen\b/, "the reasoning row stays shut");
    assert.doesNotMatch(a, /Full details|activity-script-detail/);
  });

  it("opens one nested group without opening that group in the other feed", () => {
    assert.ok(stateStep, "the turn has a state-change group");
    const feeds = feedsOf(
      rendered({
        feeds: { [feedA]: true, [feedB]: true },
        groups: [groupKey(feedA, stateStep.id)],
        thoughts: []
      })
    );
    const a = feeds.get("Plan msg-1a");
    const b = feeds.get("Plan msg-1b");
    const c = feeds.get("Plan msg-2");
    assert.match(buttonFor(a, "State changed"), /aria-expanded="true"/);
    assert.match(buttonFor(a, "run_travel_code"), /aria-expanded="false"/);
    assert.match(buttonFor(b, "State changed"), /aria-expanded="false"/);
    assert.match(buttonFor(b, "run_travel_code"), /aria-expanded="false"/);
    assert.match(a, /aria-label="Full details"/);
    assert.doesNotMatch(b, /Full details/);
    assert.doesNotMatch(a + b, /activity-script-detail/);
    assert.ok(!c.startsWith(" expanded"));
    const thought = a.match(/<details\b[^>]*>/)?.[0] ?? "";
    assert.doesNotMatch(thought, /\bopen\b/);
  });
});

// Prose sent to a script handler came back as a type checker's traceback, one block per token,
// and the chat drew it as a single wrapped paragraph that filled the pane.
describe("a script error reply", () => {
  const block = (token, column) =>
    `error[unresolved-reference]: Name \`${token}\` used when not defined\n --> main.py:1:${column}\n  |\n1 | ok run it now\n  | ^^`;
  const text = `Script error (MontyTypingError: ${["ok", "run", "it", "now"].map(block).join("\n\n")})`;
  const items = [user(1, "ok run it now"), reply(1, text)];
  const key = groupKey("chat-a-1", "script-error");

  it("is one error line that opens to the output as a code block", () => {
    const shut = articles(chat({ items }))[1];
    assert.match(shut, /<div class="trace-row[^"]*glyph-error/, "the error glyph and tone");
    assert.match(shut, /<button[^>]*class="trace-line disclosure[^"]*"[^>]*aria-expanded="false"/);
    assert.match(shut, /class="trace-status[^"]*">Script error</);
    assert.match(shut, /class="trace-note[^"]*"[^>]*>MontyTypingError: error\[unresolved-reference\]: Name `ok` used when not defined</);
    assert.doesNotMatch(shut, /md-code-block|Name `now`/, "the rest stays folded");
    assert.doesNotMatch(shut, /aria-label="Copy message"/, "the code block owns the copy");

    seedDisclosure({ groups: [key] });
    let open;
    try {
      open = articles(chat({ items }))[1];
    } finally {
      seedDisclosure(null);
    }
    assert.match(open, /aria-expanded="true"/);
    assert.match(open, /<pre class="md-code-block[^"]*"><code[^>]*>[\s\S]*Name `now` used[\s\S]*<\/code><\/pre>/);
    assert.doesNotMatch(open, /code-language-tag/, "plain output, not labelled as a language");
    assert.match(open, /aria-label="Copy code"/);
  });
});
