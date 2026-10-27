// Every handler is listed by its own name in the `/` picker and taken in on its card, whatever its
// schema. A handler that was one required enum used to be pulled out into an icon button named by
// its field, so two handlers sharing an input model (a playpen agent's `grant_access` and
// `revoke_access`, both `AccessRequest`) drew as two identical "Table: not set" icons, missing
// from the list. The open picker can't be reached from SSR (no DOM here), so its source of rows
// is read from source; the rest is checked as rendered.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import AgentChatPanel from "./AgentChatPanel.svelte";
import { composerHandler } from "$lib/state/handlerRouting.ts";

const handler = (name, properties, required) => ({
  name,
  description: "",
  parameters: { type: "object", properties, required },
  output: {},
  mid_turn: "accept",
  model_callable: false
});
const enumHandler = (name, field, choices, title = field) =>
  handler(name, { [field]: { type: "string", title, enum: choices } }, [field]);
const accepted = (handlerName, payload, id = "m1") => ({
  id, index: 1, ordinal: 1, turnNumber: 1, sourceTurnNumber: 1, turnId: "t1", timestamp: 1,
  event: "message_accepted", actor: "user", tone: "queue", label: "", citations: [],
  message: { handler: handlerName, payload }
});

const generate = handler("generate", { text: { type: "string", title: "Text" } }, ["text"]);
const setModel = enumHandler("set_model", "model", ["gemini-3.8-flash", "gemini-3.1-flash-lite"]);
const travelPolicy = enumHandler("set_approval_policy", "posture", ["human_approval_only", "allow_safe", "auto_mode"], "Posture");
const tables = ["risk.loan_applications", "risk.credit_scores"];
const grantAccess = enumHandler("grant_access", "table", tables, "Table");
const revokeAccess = enumHandler("revoke_access", "table", tables, "Table");
const panel = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");

const composer = (agentInterface, logs = []) =>
  stripComments(
    render(AgentChatPanel, {
      props: { items: [], logs, agentLabel: "Planner", sessionId: "wf-root", agentInterface }
    }).body
  );
const allButtons = (body) => [...body.matchAll(/<button[^>]*choice-button[\s\S]*?<\/button>/g)].map((m) => m[0]);
/* The composer's own, not a card's switcher between form handlers, which shares its look. */
const handlerButton = (body) =>
  allButtons(body).find((button) => button.includes("handler-button") && !button.includes("action-switch"));
const card = (body) => body.match(/<article class="pending-card[^"]*" aria-label="Action: [\s\S]*?<\/article>/)?.[0] ?? "";

describe("every handler, by name", () => {
  it("draws no control per handler beside the handler button, whatever the schema", () => {
    const body = composer([generate, setModel, travelPolicy, grantAccess, revokeAccess]);
    const composerControls = body.match(/<div class="composer-controls[\s\S]*?<\/form>/)?.[0] ?? "";
    assert.deepEqual(
      allButtons(composerControls).map((b) => b.match(/aria-label="([^"]*)"/)[1]),
      ["Handler: generate"],
      "no icon named by a field"
    );
    assert.doesNotMatch(body, /Table: not set|Model: not set|Policy: not set|choice-menu/);
    assert.match(body, /<form class="composer[\s\S]*<textarea[^>]*placeholder="Message"/);
  });

  it("lists every handler in the picker, from the one source the composer uses", () => {
    assert.match(panel, /buildPickerItems\(pickerOpen, targets, handlers, pickerDraft, showTargetPicker\)/);
    assert.match(panel, /const handlers = \$derived\(activeTarget\?\.agentInterface \?\? \[\]\);/);
    assert.doesNotMatch(panel, /choiceField|choiceHandlers/, "nothing pulls a handler out of the list");
  });

  it("takes a one-enum handler on its card, named for the handler, its options in the form", () => {
    const body = card(composer([revokeAccess]));
    assert.match(body, /aria-label="Action: revoke_access"/);
    for (const table of tables) assert.ok(body.includes(table), table);
    assert.match(body, /aria-label="Send revoke_access"/);
  });

  it("keeps an explicit pick of any handler, and a text handler as the default", () => {
    assert.equal(composerHandler([setModel, generate], "set_model")?.name, "set_model");
    assert.equal(composerHandler([grantAccess, revokeAccess], "revoke_access")?.name, "revoke_access");
    assert.equal(composerHandler([setModel, generate, travelPolicy], null)?.name, "generate");
  });

  // Default IconButton chrome painted a filled box on every composer control.
  // At rest the border stays 1px and only its color drops, so a reveal cannot shift the row.
  it("rests the handler button without a box until it is hovered, focused, open, or off-default", () => {
    const rest = panel.match(
      /\.composer-controls :global\(\.choice-button\) \{\s*border-color: transparent;\s*background: transparent;\s*color: var\(--text-3\);\s*\}/
    );
    assert.ok(rest, "rest keeps the border width and uses the quiet chip's dim token");
    assert.doesNotMatch(rest[0], /border:\s*(?:0|none)|border-width:\s*0/);
    assert.doesNotMatch(
      panel,
      /\.(?:composer|handler-form):is\(:hover, :focus-within\)[^{]*choice-button/,
      "composer hover and focus-within leave the hairline off and the icon dim"
    );
    assert.match(
      panel,
      /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*\.composer-controls :global\(\.choice-button\) \{\s*transition: none;\s*\}/
    );
    assert.match(panel, /\.composer:not\(\.expanded\) textarea::placeholder \{[^}]*white-space: nowrap/, "a wrapped placeholder shows the scrollbar track");
  });
});

// The handler picker was a full-width chip reading "ask → Monty (conversational, subagent)",
// and script mode added a row saying "Runs this Python as written."; at the narrowest pane
// both wrapped and truncated. The handler is now an icon in the control row.
describe("composer handler button", () => {
  const execute = handler("execute", { script: { type: "string", title: "Script" } }, ["script"]);
  const book = handler("book", { kind: { type: "string", enum: ["flight", "hotel"] }, city: { type: "string" } }, ["kind", "city"]);

  it("is an icon whose name and tip carry the label, with the script hint read out but not shown", () => {
    const body = composer([execute, setModel, travelPolicy]);
    const button = handlerButton(body);
    assert.ok(button, "the handler control is in the control row");
    assert.match(button, /aria-haspopup="listbox"/);
    assert.match(button, /aria-expanded="false"/);
    assert.match(button, /aria-label="Handler: execute"/);
    assert.match(button, /data-tip="Handler: execute\. Runs this Python as written\./);
    assert.deepEqual([...button.matchAll(/lucide-([a-z-]+)"/g)].map((m) => m[1]), ["square-code"]);
    assert.doesNotMatch(button, />\s*[^<\s][^<]*</, "no visible label");
    const composerControls = body.match(/<div class="composer-controls[\s\S]*?<\/form>/)?.[0] ?? "";
    assert.deepEqual(allButtons(composerControls).map((b) => b.match(/aria-label="([^"]*)"/)[1]), ["Handler: execute"]);

    const describedBy = button.match(/aria-describedby="([^"]+)"/)?.[1];
    assert.ok(describedBy, "the button is described by the hint");
    assert.match(body, new RegExp(`<textarea[^>]*aria-describedby="${describedBy}"`), "and so is the field");
    assert.match(body, new RegExp(`<span id="${describedBy}" class="visually-hidden">Runs this Python as written\\.</span>`));
    const asText = [...body.matchAll(/<([a-z]+)([^>]*)>[^<]*Runs this Python/g)].map((m) => m[2]);
    assert.deepEqual(asText, [` id="${describedBy}" class="visually-hidden"`], "never drawn as text");
    assert.doesNotMatch(body, /target-chip|composer-status|Choose message handler/, "the old picker chip is gone");
  });

  it("picks an icon per text handler kind, keeps one-handler agents to an icon, and leaves form handlers to their card", () => {
    const icon = (body) => handlerButton(body).match(/lucide-([a-z-]+)"/)[1];
    assert.equal(icon(composer([generate])), "message-square-text", "a single handler is still an icon with its tip");
    assert.equal(icon(composer([execute])), "square-code");
    const form = composer([book, setModel]);
    assert.equal(handlerButton(form), undefined, "nothing to write in the box, so no handler button on it");
    assert.match(form, /aria-label="Action: book"/, "the schema handler is the queue's card");
  });

  it("keeps a long target name out of the row and in the name, tip, and menu titles", () => {
    const long = "M".repeat(120);
    const body = render(AgentChatPanel, {
      props: {
        items: [], logs: [], agentLabel: "Planner", sessionId: "wf-root", agentInterface: [generate],
        messageTargets: [
          { workflowId: "wf-root", role: "parent", label: "Planner", agentInterface: [generate] },
          { workflowId: "wf-sub", role: "subagent", label: long, agentInterface: [generate] }
        ]
      }
    }).body;
    const button = handlerButton(stripComments(body));
    assert.match(button, /aria-label="Handler: generate → Planner"/);
    assert.doesNotMatch(body, new RegExp(`>[^<]*${long}`), "never as visible text while the list is shut");
    assert.match(panel, /class=\{\["picker-row target"[\s\S]*?title=\{item\.target\.label\}/, "a truncated target row keeps its full name");
    assert.match(panel, /class=\{\["picker-row"[\s\S]*?title=\{`\$\{item\.handler\.name\}/, "a truncated handler row keeps its full name");
  });

  // Agent DAG Studio declares `execute` before `generate`. A user who typed prose after the
  // agent wrote a script sent it as Python, with only a dim icon saying so.
  it("keeps the prose handler after a script update and reveals any handler that is not the default", () => {
    const scriptUpdate = {
      ...accepted("generate", {}, "cb1"), event: "callback_requested", actor: "agent",
      toolName: "write_code", toolId: "call-1", message: undefined
    };
    const body = stripComments(render(AgentChatPanel, {
      props: {
        items: [], logs: [scriptUpdate], agentLabel: "Studio", sessionId: "wf-root",
        agentInterface: [execute, generate]
      }
    }).body);
    const button = handlerButton(body);
    assert.match(button, /aria-label="Handler: generate"/);
    assert.doesNotMatch(button, /\brevealed\b/, "the default handler rests dim");
    assert.doesNotMatch(body, /class="composer[^"]*code-input/);
    assert.match(body, /<textarea[^>]*placeholder="Message"/);

    const writes = [...panel.matchAll(/selectedHandlers\[[^\]]+\] =/g)];
    assert.equal(writes.length, 1, "only a pick stores a handler; a resolved default is never saved");
    assert.match(panel.match(/function selectHandler[\s\S]*?\n {2}\}/)[0], /pickHandler\(handler\.name\);/);
    assert.match(panel.match(/function pickHandler[\s\S]*?\n {2}\}/)[0], /selectedHandlers\[handlerSelectionKey\] = name;/);
    assert.match(
      panel.match(/function defaultPickerSelectionIndex[\s\S]*?\n {2}\}/)[0],
      /const current = items\.findIndex\(\(item\) => item\.kind === "handler" && isCurrentPickerItem\(item\)\);/,
      "a typed / highlights the current handler, not the first declared"
    );
    assert.match(panel, /offDefaultHandler = \$derived\(\s*selectedHandler != null && selectedHandler\.name !== composerHandler\(textHandlers, null\)\?\.name/);
    assert.match(panel, /class=\{`choice-button handler-button\$\{offDefaultHandler \? " revealed" : ""\}/);
    assert.match(
      panel,
      /choice-button\[aria-expanded="true"\]\),\s*\.composer-controls :global\(\.icon-button\.handler-button\.revealed\) \{\s*border-color: var\(--border\);\s*background: transparent;\s*color: var\(--text-2\);/,
      "an off-default handler wears the revealed hairline and full icon"
    );
  });

  // Monty's only text handler says it runs scripts "via a script-runner subagent"; the composer
  // read that prose as a script field and showed "Python script" to someone about to chat.
  it("keeps a chat handler that only mentions scripts in its description a message box", () => {
    const ask = { ...generate, name: "ask", description: "Chat; it runs Python via a script-runner subagent." };
    const body = composer([ask, setModel]);
    assert.match(body, /<textarea[^>]*placeholder="Message"/);
    assert.doesNotMatch(body, /class="composer[^"]*code-input/);
    const button = handlerButton(body);
    assert.equal(button.match(/lucide-([a-z-]+)"/)[1], "message-square-text");
    assert.doesNotMatch(button, /\brevealed\b/);
  });

  it("styles the composer as code whenever the handler takes a script", () => {
    const body = composer([execute]);
    assert.match(body, /<form class="composer[^"]*\bexpanded\b[^"]*\bcode-input\b/);
    assert.match(body, /<textarea[^>]*placeholder="Python script"/);
    assert.match(body, /<textarea[^>]*spellcheck="false"/);
    assert.match(panel, /\.composer\.code-input \{[^}]*font-family: var\(--font-mono\);/);
    assert.match(panel, /"code-input": scriptInput/);
  });

  // Agent DAG Studio writes a script or prose in the same box: with two text handlers the
  // control names the one typing goes to. A form handler is never one of them.
  it("names the text handler when there is another to switch to, and only then", () => {
    const visible = (button) => button?.match(/class="handler-name[^"]*"[^>]*>([^<]*)</)?.[1];
    const both = handlerButton(composer([execute, generate]));
    assert.equal(visible(both), "generate", "the current handler's name is drawn, not only its tip");
    assert.match(both, /\blabeled\b/);
    assert.match(both, /Type \/ to choose/);
    assert.equal(visible(handlerButton(composer([generate, book]))), undefined, "one text handler: an icon, the form is a card");
    assert.equal(composerHandler([handler("new_game", { first: { type: "boolean" } }, []), book], null)?.name, "book", "input beats a command");

    const long = handler(`write_${"x".repeat(300)}`, { text: { type: "string" } }, ["text"]);
    assert.match(handlerButton(composer([long, generate])), /class="handler-name fade-end/);
    assert.match(panel, /onclick=\{\(\) => selectHandler\(item\.handler\)\}/, "every listed handler, form ones too, is pickable");
  });

  it("opens the handler list: focus moves in, arrows move, Escape hands back", () => {
    assert.match(panel, /onclick=\{togglePicker\}/);
    assert.match(panel, /id="handler-menu"\s+role="listbox"\s+tabindex="-1"/);
    assert.match(panel, /\{@attach pickerButtonOpen && focusOnOpen\}/);
    assert.match(panel, /dismissable\(\{ ondismiss: closePicker, keep: "\.handler-button" \}\)/);
    const keys = panel.match(/function handlePickerKeydown[\s\S]*?\n {2}\}/)[0];
    assert.match(keys, /ArrowDown[\s\S]*Home[\s\S]*Enter/);
    assert.doesNotMatch(keys, /Escape/, "Escape is the dismissable layer's: close and hand focus back");
    const close = panel.match(/function closePicker[\s\S]*?\n {2}\}/)[0];
    assert.match(close, /if \(pickerButtonOpen\) \{\s*pickerButtonOpen = false;\s*return;/, "no steal back to the text box");
  });
});
