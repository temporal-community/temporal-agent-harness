import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { composerHandler, takesScript } from "./handlerRouting.ts";

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
