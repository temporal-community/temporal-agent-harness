// Tests for generateAgentTypes. The golden is generated from the Python side's golden schema
// (tests/harness/golden/tictactoe.schema.json), so the two goldens pin the whole pipeline from
// agent class to TypeScript. Regenerate this one after an intended change with:
//   npm run build && node dist/src/cli.js ../../tests/harness/golden/tictactoe.schema.json \
//       -o test/golden/TicTacToeAgent.ts

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  generateAgentTypes,
  generateProtocolTypes,
  type AgentSchemaDocument
} from "../src/generate.ts";

// Compiled to dist/test/, so the package root is two levels up.
const packageRoot = new URL("../../", import.meta.url);
const read = (path: string, base: URL = packageRoot) => readFileSync(new URL(path, base), "utf8");

function doc(overrides: Partial<AgentSchemaDocument> = {}): AgentSchemaDocument {
  return {
    agent: "EchoAgent",
    source: "tests:EchoAgent",
    handlers: {
      echo: {
        description: "Echo the message back.",
        mid_turn: "reject",
        input: { $ref: "#/$defs/Echo-Input" },
        output: { $ref: "#/$defs/Echo-Output" }
      }
    },
    states: {},
    $defs: {
      "Echo-Input": { type: "object", title: "Echo", properties: { text: { type: "string" } } },
      "Echo-Output": {
        type: "object",
        title: "Echo",
        properties: { text: { type: "string" } },
        required: ["text"]
      }
    },
    ...overrides
  };
}

test("tic-tac-toe matches the golden", async () => {
  const schema = JSON.parse(read("../../tests/harness/golden/tictactoe.schema.json"));
  assert.equal(await generateAgentTypes(schema), read("test/golden/TicTacToeAgent.ts"));
});

test("an input/output pair of one model becomes two interfaces", async () => {
  const out = await generateAgentTypes(doc());
  assert.match(out, /export interface EchoInput \{\n {2}text\?: string;\n\}/);
  assert.match(out, /export interface EchoOutput \{\n {2}text: string;\n\}/);
  assert.match(out, /echo: \{ input: EchoInput; output: EchoOutput \};/);
});

test("an agent with no declared state has an empty states map", async () => {
  assert.match(await generateAgentTypes(doc()), /states: Record<never, never>;/);
});

test("property titles never become named types", async () => {
  const out = await generateAgentTypes(
    doc({
      $defs: {
        ...doc().$defs,
        "Echo-Input": {
          type: "object",
          title: "Echo",
          properties: { mode: { title: "Mode", enum: ["a", "b"], type: "string" } }
        }
      }
    })
  );
  assert.match(out, /mode\?: "a" \| "b";/);
  assert.doesNotMatch(out, /export type Mode/);
});

test("a handler description cannot close its own comment", async () => {
  const handlers = { echo: { ...doc().handlers.echo!, description: "Ends early */ oops." } };
  assert.match(await generateAgentTypes(doc({ handlers })), /\/\*\* Ends early \*\\\/ oops\. \*\//);
});

test("the mapping type can be renamed, and may not reuse a model's name", async () => {
  assert.match(await generateAgentTypes(doc(), { typeName: "Echoer" }), /export interface Echoer \{/);
  await assert.rejects(generateAgentTypes(doc(), { typeName: "EchoInput" }), /also the name of/);
});

test("two schema names that would generate one type name are rejected", async () => {
  const $defs = { ...doc().$defs, EchoInput: { type: "object" as const } };
  await assert.rejects(generateAgentTypes(doc({ $defs })), /would both be generated as EchoInput/);
});

test("the protocol's payloads form one union, and their types another", async () => {
  const out = await generateProtocolTypes({
    event: { $ref: "#/$defs/AgentEvent" },
    stream_items: [{ $ref: "#/$defs/Ping" }, { $ref: "#/$defs/Pong" }],
    $defs: {
      AgentEvent: {
        type: "object",
        properties: {
          agent_id: { type: "string" },
          event: { oneOf: [{ $ref: "#/$defs/Ping" }, { $ref: "#/$defs/Pong" }] }
        },
        required: ["agent_id", "event"]
      },
      Ping: { type: "object", properties: { type: { const: "ping" } }, required: ["type"] },
      Pong: {
        type: "object",
        properties: {
          type: { const: "pong" },
          reason: { $ref: "#/$defs/Reason", description: "Why, per ``Ping``." }
        },
        required: ["type", "reason"]
      },
      Reason: { type: "string", enum: ["late", "early"] }
    }
  });
  assert.match(out, /export type AgentStreamItem =\n {2}\| Ping\n {2}\| Pong;/);
  assert.match(out, /export type AgentEventType = AgentStreamItem\["type"\];/);
  // A `$ref` with a description is the referenced type, documented, not a new named type.
  assert.match(out, /\/\*\*\n {3}\* Why, per `Ping`\.\n {3}\*\/\n {2}reason: Reason;/);
  assert.doesNotMatch(out, /Reason1/);
});
