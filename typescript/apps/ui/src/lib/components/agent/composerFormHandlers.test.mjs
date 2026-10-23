// A handler that takes a form (anything but one string) is a card in the attention queue, drawn
// and counted like an approval or a callback, and the composer stays a message field. Tic-tac-toe
// declares `new_game` and `play`; its move used to be a form inside the composer, behind an
// unlabeled icon, with no box left to type in.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import AgentChatPanel from "./AgentChatPanel.svelte";

const handler = (name, properties, required = []) => ({
  name,
  description: "",
  parameters: { type: "object", properties, required },
  output: {},
  mid_turn: "enqueue",
  model_callable: false
});
const newGame = handler("new_game", { agent_goes_first: { type: "boolean", title: "Agent Goes First", default: false } });
const play = handler("play", { cell: { type: "integer", title: "Cell", minimum: 1, maximum: 9 } }, ["cell"]);
const ask = handler("ask", { text: { type: "string", title: "Text" } }, ["text"]);
const book = handler("book", { city: { type: "string" }, nights: { type: "integer" } }, ["city"]);
const setModel = handler("set_model", { model: { type: "string", enum: ["a", "b"] } }, ["model"]);
const panel = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");

const row = (event, turnNumber, extra = {}) => ({
  id: `${event}-${turnNumber}`, index: turnNumber, ordinal: turnNumber, turnNumber, sourceTurnNumber: turnNumber,
  turnId: `t${turnNumber}`, timestamp: 1_789_000_000 + turnNumber, event, actor: "agent", tone: "agent", label: "",
  citations: [], ...extra
});
const composer = (agentInterface, logs = []) =>
  stripComments(
    render(AgentChatPanel, {
      props: { items: [], logs, agentLabel: "Tic-tac-toe", sessionId: "wf-root", agentInterface }
    }).body
  );
const card = (body) => body.match(/<article class="pending-card[^"]*" aria-label="Action: [\s\S]*?<\/article>/)?.[0] ?? "";
const composerForm = (body) => body.match(/<form class="composer[\s\S]*?<\/form>/)?.[0] ?? "";

describe("form handlers as attention queue cards", () => {
  it("offers the move as a queue card, counted in the chip as an action", () => {
    const body = composer([newGame, play, setModel]);
    const front = card(body);
    assert.match(front, /aria-label="Action: play"/, "the handler that takes input, not the command");
    assert.match(front, /role="radiogroup" aria-labelledby="action-play-cell-label"/, "its fields from the schema, the cell as buttons");
    assert.equal(front.match(/role="radio"/g)?.length, 9);
    assert.match(front, /aria-label="Send play"/);
    const switcher = front.match(/<button[^>]*\baction-switch\b[^>]*>[\s\S]*?<\/button>/)?.[0] ?? "";
    assert.match(switcher, /aria-label="Action: play"/, "several form handlers: one card, switched in place");
    assert.match(switcher, /aria-haspopup="listbox"/);
    assert.match(switcher, /class="handler-name fade-end[^"]*">play</, "the composer's named control, fading a long name");
    assert.doesNotMatch(front, /<select/, "not a native select, which can only ellipsize");
    assert.match(body, /1 needs you/);
    assert.match(body, / · 1 action</);
    assert.match(front, /Before the first turn/, "no 'your turn': only what the run shows");
  });

  it("deals one card with no switcher for a lone form handler, and one with only Send for no fields", () => {
    assert.doesNotMatch(card(composer([play])), /action-switch/);
    const ping = card(composer([handler("ping", {})]));
    assert.match(ping, /aria-label="Send ping"/);
    assert.doesNotMatch(ping, /schema-field/);
  });

  it("offers no card while a turn is under way, or by default beside a handler that takes text", () => {
    assert.equal(card(composer([newGame, play], [row("turn_started", 1)])), "");
    assert.match(card(composer([newGame, play], [row("turn_started", 1), row("turn_end", 1)])), /After turn 1/);
    assert.equal(card(composer([ask, book])), "", "a chat agent's form is a pick away, not a request");
  });

  it("never renders a handler form in the composer, which stays a message field", () => {
    for (const agent of [[newGame, play], [ask, book], [handler("ping", {})], [play]]) {
      const box = composerForm(composer(agent));
      assert.match(box, /<textarea/, agent.map((h) => h.name).join());
      assert.doesNotMatch(box, /schema-field|handler-form|<select|type="number"/);
    }
    assert.doesNotMatch(panel, /handler-form/);
  });

  // The box once took "5" as play(cell=5); the move now lives only on the card.
  it("never sends typed text to a form handler, and says where input goes", () => {
    const body = composer([newGame, play]);
    const box = composerForm(body);
    assert.match(box, /placeholder="Message"/);
    assert.doesNotMatch(box, /<textarea[^>]*disabled/, "still typeable");
    assert.match(box, /<button[^>]*data-tip="Use the card above to send play\."[^>]*disabled/, "Send says why it waits");
    assert.match(body, /class="visually-hidden">Use the card above to send play\.</, "and the box is described by it");
    assert.doesNotMatch(panel, /fillTarget|for \$\{[^}]*handler\.name\}`/);
  });

  // Typing is never thrown away: only a send that went through clears the box.
  it("keeps typed text when nothing takes it, through a failed send, and a switch on the card", () => {
    const send = panel.match(/async function sendMessage[\s\S]*?\n {2}\}/)[0];
    assert.match(send, /!handler \|\| !field \|\|[^)]*\) \{\s*return;/, "no text handler: nothing sent, the text stays");
    assert.match(send, /const sent = await dispatchMessage\(handler, payload, text\);\s*if \(sent\) \{\s*\/\/[^\n]*\n\s*if \(draft === text\) dissolveClear/);
    assert.equal(send.match(/draft = ""/g)?.length, 1, "cleared in one place, after a send");
    assert.doesNotMatch(panel.match(/function switchAction[\s\S]*?\n {2}\}/)[0], /draft/);
    const cardSource = readFileSync(new URL("./HandlerActionCard.svelte", import.meta.url), "utf8");
    assert.match(cardSource, /sent = await onsend!\(payload\);\s*if \(!sent\) error =/, "a failed card send keeps its values");
  });
});
