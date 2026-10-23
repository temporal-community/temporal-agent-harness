#!/usr/bin/env node
// harness-codegen <schema.json | -> [-o <out.ts>] [--name TypeName] [--json-schemas] [--protocol]

import { readFileSync, writeFileSync } from "node:fs";
import { parseArgs } from "node:util";

import {
  generateAgentTypes,
  generateProtocolTypes,
  type AgentSchemaDocument,
  type ProtocolSchemaDocument
} from "./generate.ts";

const USAGE = `usage: harness-codegen <schema.json | -> [-o <out.ts>] [--name <TypeName>] [--json-schemas] [--protocol]

Reads the document \`temporal-agent-harness schema module:Class\` prints (from a file, or
stdin for -) and writes TypeScript types for the agent: one interface per model, and a
mapping type naming each handler's input and output and each declared state. With
--json-schemas, also writes each handler's input and output JSON Schema as a constant.

With --protocol, reads \`temporal-agent-harness schema --protocol\` instead and writes the
event stream's types.`;

async function main(): Promise<void> {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      output: { type: "string", short: "o" },
      name: { type: "string" },
      protocol: { type: "boolean" },
      "json-schemas": { type: "boolean" },
      help: { type: "boolean", short: "h" }
    }
  });
  if (values.help || positionals.length !== 1) {
    console.error(USAGE);
    process.exit(values.help ? 0 : 2);
  }

  const input = positionals[0] === "-" ? readFileSync(0, "utf8") : readFileSync(positionals[0]!, "utf8");
  const doc: unknown = JSON.parse(input);
  const source = values.protocol
    ? await generateProtocolTypes(doc as ProtocolSchemaDocument)
    : await generateAgentTypes(doc as AgentSchemaDocument, {
        typeName: values.name,
        jsonSchemas: values["json-schemas"]
      });
  if (values.output === undefined) process.stdout.write(source);
  else writeFileSync(values.output, source);
}

main().catch((error: unknown) => {
  console.error(`harness-codegen: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
