// A handler that is one required enum is an icon button in the composer's control row that opens
// a list of its options; a pick sends it as the handler form would, while the text box stays the
// prompt. Pressing the button used to step the value, so a burst of presses sent a burst of
// changes. The open list can't be reached from SSR (no DOM here), so its wiring is read from
// source; the rendered button is checked as rendered.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import AgentChatPanel from "./AgentChatPanel.svelte";
import { choiceField, choiceLabel, choicePayload, composerHandler } from "$lib/state/handlerRouting.ts";

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
const policyOptions = ["human_approval_only", "allow_safe", "auto_mode"];
const travelPolicy = enumHandler("set_approval_policy", "posture", policyOptions, "Posture");
const panel = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");

const composer = (agentInterface, logs = []) =>
  stripComments(
    render(AgentChatPanel, {
      props: { items: [], logs, agentLabel: "Planner", sessionId: "wf-root", agentInterface }
    }).body
  );
const allButtons = (body) => [...body.matchAll(/<button[^>]*choice-button[\s\S]*?<\/button>/g)].map((m) => m[0]);
const buttons = (body) => allButtons(body).filter((button) => !button.includes("handler-button"));
const handlerButton = (body) => allButtons(body).find((button) => button.includes("handler-button"));
const names = (body) => buttons(body).map((button) => button.match(/aria-label="([^"]*)"/)?.[1]);

describe("composer choice buttons", () => {
  it("puts model and policy in the bar as buttons and keeps the prompt a text box", () => {
    const body = composer([generate, setModel, travelPolicy]);

    assert.deepEqual(names(body), ["Model: not set", "Policy: not set"], "short names, never handler ids");
    assert.equal(choiceLabel(generate, { name: "approval_mode", title: "approval_mode" }), "Approval mode");
    assert.match(body, /<form class="composer[\s\S]*<textarea/);
    assert.doesNotMatch(body, /handler-form|<select|role="combobox"|choice-menu/, "no form, no menu");
    assert.equal(composerHandler([setModel, generate, travelPolicy], "set_model")?.name, "generate");
  });

  it("opens a listbox of every option instead of sending on a press", () => {
    const body = composer([generate, travelPolicy]);
    const [button] = buttons(body);
    assert.match(button, /aria-haspopup="listbox"/);
    assert.match(button, /aria-expanded="false"/);
    assert.doesNotMatch(body, /role="listbox"/, "closed until pressed");

    assert.match(panel, /onclick=\{\(\) => toggleChoice\(choice\)\}/, "a press opens, it never sends");
    assert.doesNotMatch(panel.match(/function toggleChoice[\s\S]*?\n {2}\}/)[0], /dispatchMessage|pickChoice/);
    assert.match(panel, /role="listbox"[\s\S]*aria-activedescendant/);
    assert.match(
      panel,
      /\{#each choice\.field\.choices \?\? \[\] as option, index \(option\)\}[\s\S]*?role="option"[\s\S]*?aria-selected=\{option === choice\.value\}/,
      "one option per enum value, the current one selected"
    );
    assert.match(panel, /\.picker-row\.selected \{\s*background: var\(--selected-bg\);/, "selection is a tint");
  });

  it("sends a picked row as the handler form's payload and closes the list", () => {
    assert.deepEqual(choicePayload(choiceField(setModel), "gemini-3.1-flash-lite"), { model: "gemini-3.1-flash-lite" });
    const withNote = handler(
      "set_policy",
      { posture: { type: "string", enum: ["auto_mode"] }, note: { type: "string" } },
      ["posture"]
    );
    assert.deepEqual(choicePayload(choiceField(withNote), "auto_mode"), { posture: "auto_mode" });
    assert.match(panel, /onclick=\{\(\) => void pickChoice\(choice, option\)\}/);
    const pick = panel.match(/async function pickChoice[\s\S]*?\n {2}\}/)[0];
    assert.match(pick, /^[^\n]*\{\s*openChoiceName = null;/, "closes before anything else");
    assert.match(pick, /dispatchMessage\(choice\.handler, payload,/, "through the form's own send");
  });

  // Each press used to send at once, so ten fast presses left ten "policy changed" rows.
  it("lets one choice send be in flight at a time and undoes a failed one", () => {
    const pick = panel.match(/async function pickChoice[\s\S]*?\n {2}\}/)[0];
    const blocked = panel.match(/function choiceBlocked[\s\S]*?\n {2}\}/)[0];
    assert.match(blocked, /choiceSending \|\|/, "a send in flight blocks every choice");
    assert.match(panel.match(/function toggleChoice[\s\S]*?\n {2}\}/)[0], /if \(choiceBlocked\(choice\)\) return;/, "no list while sending");
    assert.match(pick, /option === choice\.value \|\| choiceBlocked\(choice\)\) return;/, "the current value or a second pick is a no-op");
    assert.ok(pick.indexOf("choiceSending = true") < pick.indexOf("await dispatchMessage"), "claimed before the await");
    assert.match(pick, /finally \{\s*choiceSending = false;\s*if \(!sent && sentChoices\[key\]\?\.value === option\)/, "a failed send puts the old value back");
    assert.match(pick, /if \(previous\) sentChoices\[key\] = previous;\s*else delete sentChoices\[key\];/);
  });

  it("closes on Escape or an outside press without sending", () => {
    assert.match(panel, /dismissable\(\{ ondismiss: \(\) => \(openChoiceName = null\), keep: "\.choice-button" \}\)/);
    const keys = panel.match(/function handleChoiceKeydown[\s\S]*?\n {2}\}/)[0];
    assert.match(keys, /event\.key === "Enter" \|\| event\.key === " "\) \{[\s\S]*?pickChoice/, "only Enter or Space on a row picks");
    assert.doesNotMatch(keys, /Escape/, "Escape is the dismissable layer's: close and hand focus back");
    // Held or repeated Enter opens the list and then lands on it: with no value yet, that
    // press must not pick the first row.
    assert.match(panel, /choiceIndex = choice\.field\.choices!\.indexOf\(choice\.value \?\? ""\);/);
    assert.match(keys, /if \(choiceIndex < 0\) openChoiceName = null;\s*else void pickChoice/);
  });

  // The travel agent's policy field is titled "Posture", and with no default the chip printed
  // that title. The control is now the icon alone; the value lives in its name and tip.
  it("is an icon alone, names the policy Policy, and shows the last sent value", () => {
    const untitled = handler("set_effort", { reasoning_effort: { type: "string", enum: ["low", "high"] } }, ["reasoning_effort"]);
    const body = composer([generate, travelPolicy, setModel, untitled], [accepted("set_model", { model: "gemini-3.8-flash" })]);

    assert.deepEqual(names(body), ["Policy: not set", "Model: gemini-3.8-flash", "Reasoning Effort: not set"]);
    assert.doesNotMatch(body, /Posture/, "not in the button, its name, or its tip");
    const icons = buttons(body).map((button) => [...button.matchAll(/lucide-([a-z-]+)"/g)].map((m) => m[1]));
    assert.deepEqual(icons, [["shield-check"], ["cpu"], ["sliders-horizontal"]], "one icon each, no chevron");
    for (const button of buttons(body)) {
      assert.doesNotMatch(button, />\s*[^<\s][^<]*</, "no visible label");
      assert.match(button, /data-tip="[^"]+"/);
    }

    let logs = [];
    for (const [i, option] of [policyOptions[2], policyOptions[0]].entries()) {
      logs = [...logs, accepted("set_approval_policy", { posture: option }, `m${i}`)];
      assert.deepEqual(names(composer([generate, travelPolicy], logs)), [`Policy: ${option === "auto_mode" ? "Auto mode" : "Human approval only"}`]);
    }
  });

  it("adds no buttons for an agent with no single-enum handler", () => {
    const book = handler(
      "book",
      { kind: { type: "string", enum: ["flight", "hotel"] }, city: { type: "string" } },
      ["kind", "city"]
    );
    const upload = handler("upload", { file: { type: "string", contentEncoding: "base64" } }, ["file"]);
    for (const other of [book, upload, generate]) assert.equal(choiceField(other), null, other.name);
    assert.deepEqual(names(composer([generate, book])), []);
  });

  it("keeps a long value whole in the name and tip, and never squeezes the button", () => {
    const long = `model-${"x".repeat(300)}`;
    const many = enumHandler("set_model", "model", [long, "m-1"]);
    const body = composer([generate, many], [accepted("set_model", { model: long })]);
    assert.deepEqual(names(body), [`Model: ${long}`]);
    assert.ok(body.includes(`data-tip="Model: ${long}"`));
    assert.match(panel, /:global\(\.choice-button\) \{\s*flex: none;/);
    assert.match(panel, /\.composer:not\(\.expanded\) textarea::placeholder \{[^}]*white-space: nowrap/, "a wrapped placeholder shows the scrollbar track");
  });

  // Default IconButton chrome painted a filled box on every choice control.
  // At rest the border stays 1px and only its color drops, so a reveal cannot shift the row.
  // Field hover and focus-within used to light the box while typing; only the button itself does now.
  it("rests a choice button without a box until that button is hovered, focused, or open", () => {
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

    const reveal = panel.match(
      /\.composer-controls :global\(\.icon-button\.choice-button:hover:not\(:disabled, \[aria-disabled="true"\]\)\),\s*\.composer-controls :global\(\.icon-button\.choice-button:focus-visible\),\s*\.composer-controls :global\(\.icon-button\.choice-button\[aria-expanded="true"\]\),\s*\.composer-controls :global\(\.icon-button\.handler-button\.revealed\) \{\s*border-color: var\(--border\);\s*background: transparent;\s*color: var\(--text-2\);\s*\}/
    );
    assert.ok(reveal, "hover, keyboard focus, and an open list bring the hairline and the full icon");
    assert.match(
      panel,
      /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*\.composer-controls :global\(\.choice-button\) \{\s*transition: none;\s*\}/
    );

    const body = composer([generate, setModel, travelPolicy]);
    assert.match(body, /<textarea[^>]*placeholder="Message"/);
    assert.match(body, /<textarea[^>]*aria-label="Message /);
  });
});

// The handler picker was a full-width chip reading "ask → Monty (conversational, subagent)",
// and script mode added a row saying "Runs this Python as written."; at the narrowest pane
// both wrapped and truncated. The handler is now one more choice icon in the same row.
describe("composer handler button", () => {
  const execute = handler("execute", { script: { type: "string", title: "Script" } }, ["script"]);
  const book = handler("book", { kind: { type: "string", enum: ["flight", "hotel"] }, city: { type: "string" } }, ["kind", "city"]);

  it("is a choice icon whose name and tip carry the label, with the script hint read out but not shown", () => {
    const body = composer([execute, setModel, travelPolicy]);
    const button = handlerButton(body);
    assert.ok(button, "the handler control is a choice button");
    assert.match(button, /aria-haspopup="listbox"/);
    assert.match(button, /aria-expanded="false"/);
    assert.match(button, /aria-label="Handler: execute"/);
    assert.match(button, /data-tip="Handler: execute\. Runs this Python as written\./);
    assert.deepEqual([...button.matchAll(/lucide-([a-z-]+)"/g)].map((m) => m[1]), ["square-code"]);
    assert.doesNotMatch(button, />\s*[^<\s][^<]*</, "no visible label");
    assert.deepEqual(
      allButtons(body).map((b) => b.match(/aria-label="([^"]*)"/)[1]),
      ["Handler: execute", "Model: not set", "Policy: not set"],
      "handler first, in the same row as model and policy"
    );

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
        agentInterface: [execute, generate], editorScript: "async def main():\n    pass"
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

  it("opens the handler list like its siblings: focus moves in, arrows move, Escape hands back", () => {
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
