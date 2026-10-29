#!/usr/bin/env node
// `agent-harness-schema <module>#<Export> [-o <file>]`, the bin this package publishes: the Node
// half of Python's harness/agent_schema.py (`load_agent_class`, `dump_agent_schema`) and its
// `temporal-agent-harness schema` command. It imports one agent and writes its schema document
// (core's `agentSchema`), which an agents.toml entry's `schema` points at so
// `temporal-agent-harness serve` can read what the agent takes to start without importing it.
// Starts no workflow and needs no Temporal connection.

import { realpathSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import type { AgentWorkflow } from "@temporalio/agent-harness";
import { agentSchema } from "@temporalio/agent-harness/harness/agent-schema";

const USAGE = "usage: agent-harness-schema <module>#<Export> [-o <file>]";

export class SchemaCliError extends Error {
  override name = "SchemaCliError";
}

/** The agent exported as `<Export>` from `<module>` (a path, relative to the current directory). */
export async function loadAgent(reference: string): Promise<AgentWorkflow> {
  const hash = reference.lastIndexOf("#");
  if (hash <= 0 || hash === reference.length - 1)
    throw new SchemaCliError(`expected <module>#<Export>, got '${reference}'`);
  const path = reference.slice(0, hash);
  const name = reference.slice(hash + 1);
  const module = (await import(pathToFileURL(resolve(path)).href)) as Record<string, unknown>;
  const agent = module[name];
  if (agent === undefined) throw new SchemaCliError(`${path} has no export named '${name}'`);
  if (typeof agent !== "function" || !("definition" in agent))
    throw new SchemaCliError(`${path}#${name} is not an agent made with defineAgent`);
  return agent as AgentWorkflow;
}

/** The agent's schema document as stable, diffable JSON text, as Python's `dump_agent_schema`. */
export async function dumpAgentSchema(reference: string): Promise<string> {
  return JSON.stringify(agentSchema(await loadAgent(reference), reference), null, 2) + "\n";
}

export async function main(argv: readonly string[]): Promise<void> {
  const { values, positionals } = parseArgs({
    args: [...argv],
    allowPositionals: true,
    options: { output: { type: "string", short: "o" }, help: { type: "boolean", short: "h" } },
  });
  if (values.help) {
    process.stdout.write(`${USAGE}\n`);
    return;
  }
  if (positionals.length !== 1) throw new SchemaCliError(USAGE);
  const text = await dumpAgentSchema(positionals[0]!);
  if (values.output === undefined) process.stdout.write(text);
  else writeFileSync(values.output, text);
}

// Run as a program, including through the symlink npm installs for the bin.
const entry = process.argv[1];
if (entry !== undefined && import.meta.url === pathToFileURL(realpathSync(entry)).href) {
  main(process.argv.slice(2)).catch((e: unknown) => {
    process.stderr.write(
      `agent-harness-schema: ${e instanceof SchemaCliError ? e.message : String(e)}\n`,
    );
    process.exitCode = 1;
  });
}
