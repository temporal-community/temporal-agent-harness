import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { actionQueue, composerHandler, offersActionCard, pickSettled, takesScript } from "./handlerRouting.ts";

const handler = (name, field, description = "") => ({
  name,
  description,
  parameters: {
    type: "object",
    properties: { [field]: { type: "string", title: field } },
    required: [field]
  },
  output: {},
  mid_turn: "enqueue",
  model_callable: false
});

describe("composer handler routing", () => {
  const execute = handler("execute", "script", "Run a flow script.");
  const generate = handler("generate", "text", "Describe the flow you want.");

  it("defaults prose to the uniquely natural-language handler", () => {
    assert.equal(composerHandler([execute, generate], null)?.name, "generate");
  });

  it("keeps an explicit script-handler choice", () => {
    assert.equal(composerHandler([execute, generate], "execute")?.name, "execute");
  });

  // Monty's chat handler describes itself as driving "a script-runner subagent".
  it("reads a script from the schema, never the handler's description", () => {
    const ask = handler("ask", "text", "Chat; it runs Python via a script-runner subagent.");
    assert.equal(takesScript(ask), false);
    assert.equal(takesScript(execute), true);
    assert.equal(composerHandler([execute, ask], null)?.name, "ask");
  });

  it("defaults to the script handler only when nothing else takes text", () => {
    assert.equal(composerHandler([execute], null)?.name, "execute");
    assert.equal(composerHandler([execute, handler("ask", "value")], null)?.name, "ask");
  });

  it("ignores a stale pick the agent no longer declares", () => {
    assert.equal(composerHandler([execute, generate], "run_script")?.name, "generate");
  });

  it("preserves declared order when the schemas are ambiguous", () => {
    assert.equal(
      composerHandler([handler("first", "value"), handler("second", "input")], null)?.name,
      "first"
    );
  });
});

// A form handler picked mid-turn once dealt no card until the turn ended, so a handler that
// queues or joins a running turn could be listed but never filled in.
describe("action card offer", () => {
  const book = { ...handler("book", "city"), mid_turn: "enqueue" };

  it("deals the card while idle, picked or not", () => {
    assert.equal(offersActionCard(book, null, true), true);
    assert.equal(offersActionCard({ ...book, mid_turn: "reject" }, null, true), true);
  });

  it("deals a picked card mid-turn whatever its mid_turn; the agent decides on send", () => {
    for (const mid_turn of ["enqueue", "accept", "reject"]) {
      assert.equal(offersActionCard({ ...book, mid_turn }, "book", false), true, mid_turn);
    }
  });

  it("deals no card mid-turn unpicked", () => {
    assert.equal(offersActionCard(book, null, false), false);
  });

  // A turn waiting on an approval kept the picked card behind it, so the pick looked like it did nothing.
  it("puts a picked card in front of the requests the agent is blocked on, an offered one after", () => {
    assert.deepEqual(actionQueue(["approval"], "card", true), ["card", "approval"]);
    assert.deepEqual(actionQueue(["approval"], "card", false), ["approval", "card"]);
    assert.deepEqual(actionQueue(["approval"], null, true), ["approval"]);
  });

  // set_approval_policy sent while idle opened and ended its turn between two frames, so the run
  // never read busy, the pick stayed, and the card was offered again with the same value.
  it("lets a sent pick go once its turn is under way or a newer turn has ended", () => {
    assert.equal(pickSettled(8, true, 8), false, "nothing has happened yet: the card says sent");
    assert.equal(pickSettled(8, false, 9), true, "its turn is under way");
    assert.equal(pickSettled(8, true, 9), true, "its turn already came and went");
  });
});
