// ABOUTME: The start dialog renders an agent's init data as a form, and offers a way to skip it
// only when the data is optional. Rendered on the server (no DOM here), so this checks the markup;
// opening via showModal() and submitting are the controller's and the browser's (see initData.ts
// and its tests for the payload a filled form becomes).

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import StartSessionDialog from "./StartSessionDialog.svelte";

const schema = {
  title: "MatchSettings",
  description: "Who the agent is playing.",
  type: "object",
  properties: { player_name: { type: "string", title: "Player Name" } },
  required: ["player_name"]
};

const agent = (required) => ({
  key: "tictactoe",
  label: "Tic-Tac-Toe",
  workflow_type: "TicTacToeAgent",
  task_queue: "q",
  description: "",
  init_data: { required, schema }
});

const html = (props) =>
  render(StartSessionDialog, { props: { onStart() {}, onClose() {}, ...props } }).body;

describe("StartSessionDialog", () => {
  it("renders the data model's fields for the agent being started", () => {
    const body = html({ agent: agent(true) });
    assert.match(body, /Start Tic-Tac-Toe/);
    assert.match(body, /MatchSettings/);
    assert.match(body, /Player Name/);
    assert.match(body, /Start session/);
  });

  it("offers to start without the data only when it is optional", () => {
    assert.doesNotMatch(html({ agent: agent(true) }), /Start without/);
    assert.match(html({ agent: agent(false) }), /Start without/);
  });

  it("renders no form when no agent is being started", () => {
    const body = html({ agent: null });
    assert.match(body, /<dialog/);
    assert.doesNotMatch(body, /<form/);
  });
});
