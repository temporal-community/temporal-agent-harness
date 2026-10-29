import { execFile } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { agentSchema } from "@temporalio/agent-harness/harness/agent-schema";
import { SchemaCliError, dumpAgentSchema, loadAgent } from "../../src/harness/agent-schema.ts";
import { BriefedTsAgent } from "../serving/agents.ts";

const run = promisify(execFile);
const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));
const cli = here("../../src/harness/agent-schema.js");
const compiled = here("../serving/agents.js");

describe("agent-harness-schema", () => {
  it("loads the agent a module exports under the given name", async () => {
    assert.equal(await loadAgent(`${compiled}#BriefedTsAgent`), BriefedTsAgent);
  });

  it("rejects a reference without a module and an export", async () => {
    for (const reference of ["agents.js", "#BriefedTsAgent", "agents.js#"])
      await assert.rejects(loadAgent(reference), SchemaCliError);
  });

  it("names the missing export", async () => {
    await assert.rejects(loadAgent(`${compiled}#Nope`), /has no export named 'Nope'/);
  });

  it("refuses an export that is not an agent", async () => {
    await assert.rejects(loadAgent(`${cli}#main`), /is not an agent made with defineAgent/);
  });

  it("writes the document as stable two-space JSON with a final newline", async () => {
    const reference = `${compiled}#BriefedTsAgent`;
    assert.equal(
      await dumpAgentSchema(reference),
      JSON.stringify(agentSchema(BriefedTsAgent, reference), null, 2) + "\n",
    );
  });

  it("runs as a program on a TypeScript source file and writes to -o", async () => {
    const out = join(mkdtempSync(join(tmpdir(), "schema-")), "BriefedTsAgent.schema.json");
    await run(process.execPath, [cli, "agents.ts#BriefedTsAgent", "-o", out], {
      cwd: here("../serving"),
    });
    const document = JSON.parse(readFileSync(out, "utf8")) as Record<string, unknown>;
    assert.equal(document.agent, "BriefedTsAgent");
    assert.equal(document.source, "agents.ts#BriefedTsAgent");
    assert.deepEqual(document.init_data, { data: { $ref: "#/$defs/Brief" }, required: true });
  });

  it("exits non-zero with the reason on stderr", async () => {
    await assert.rejects(
      run(process.execPath, [cli, "agents.js"]),
      (e: { code?: number; stderr?: string }) => {
        assert.equal(e.code, 1);
        assert.match(e.stderr ?? "", /^agent-harness-schema: expected <module>#<Export>/);
        return true;
      },
    );
  });
});
