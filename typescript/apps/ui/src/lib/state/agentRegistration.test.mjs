// ABOUTME: Asserts the console's agent registration: the form's rows are checked by the server's
// agents.toml rules before anything is sent, an agents.toml and the schema files it names import
// into rows (by relative path from a folder or zip, else by file name), and a download's zip holds
// an agents.toml and schema files that import back into the same registrations.

import assert from "node:assert/strict";
import { describe, it } from "vitest";
import { parse } from "smol-toml";

import {
  checkRows,
  emptyRow,
  filesFromZip,
  registryArchive,
  rowFromRegistration,
  rowsFromFiles
} from "./agentRegistration";

const SCHEMA = {
  agent: "BriefedAgent",
  init_data: { data: { $ref: "#/$defs/Brief" }, required: true },
  $defs: { Brief: { title: "Brief", type: "object", properties: {} } }
};

function row(key, fields = {}) {
  return {
    ...emptyRow(),
    key,
    workflow_type: `${key[0].toUpperCase()}${key.slice(1)}Agent`,
    task_queue: `${key}-queue`,
    label: key,
    description: `The ${key} agent.`,
    ...fields
  };
}

const REGISTRY = `
[[agents]]
key = "briefed"
workflow_type = "BriefedAgent"
task_queue = "briefed-queue"
label = "Briefed"
schema = "schemas/BriefedAgent.schema.json"
description = """
Takes a
brief."""
agent = "./agent.ts#BriefedAgent"

[[agents]]
key = "plain"
workflow_type = "PlainAgent"
task_queue = "plain-queue"
label = "Plain"
description = "Takes nothing."
`;

describe("checkRows", () => {
  it("turns complete rows into registrations, with the schema parsed", () => {
    const result = checkRows([row("briefed", { schema: JSON.stringify(SCHEMA) }), row("plain")]);

    assert.ok("registrations" in result);
    assert.deepEqual(result.registrations[0].schema, SCHEMA);
    assert.equal(result.registrations[1].schema, null);
  });

  it("names every missing field", () => {
    const result = checkRows([emptyRow()]);

    assert.ok("problems" in result);
    assert.deepEqual(Object.keys(result.problems[0]).sort(), ["description", "key", "label", "task_queue", "workflow_type"]);
  });

  it("puts a repeated key or workflow type on its later row", () => {
    const result = checkRows([row("a"), row("a", { workflow_type: "OtherAgent" }), row("b", { workflow_type: "AAgent" })]);

    assert.ok("problems" in result);
    assert.deepEqual(result.problems[0], {});
    assert.match(result.problems[1].key, /row 1/);
    assert.match(result.problems[2].workflow_type, /row 1/);
  });

  it("refuses a schema that is not JSON, not an object, or for another workflow", () => {
    const result = checkRows([
      row("a", { schema: "{" }),
      row("b", { schema: "[]" }),
      row("briefed", { workflow_type: "SomeoneElse", schema: JSON.stringify(SCHEMA) })
    ]);

    assert.ok("problems" in result);
    assert.match(result.problems[0].schema, /Not JSON/);
    assert.match(result.problems[1].schema, /JSON object/);
    assert.match(result.problems[2].schema, /"BriefedAgent", not "SomeoneElse"/);
  });
});

describe("rowsFromFiles", () => {
  it("reads a folder's agents.toml, resolving each schema against its folder", () => {
    const result = rowsFromFiles([
      { path: "project/agents.toml", text: REGISTRY },
      { path: "project/schemas/BriefedAgent.schema.json", text: JSON.stringify(SCHEMA) },
      { path: "other/BriefedAgent.schema.json", text: "{}" }
    ]);

    assert.ok("rows" in result);
    assert.deepEqual(result.problems, [{}, {}]);
    assert.deepEqual(JSON.parse(result.rows[0].schema), SCHEMA);
    assert.equal(result.rows[0].description, "Takes a brief.");
    assert.equal(result.rows[1].schema, "");
    assert.ok("registrations" in checkRows(result.rows));
  });

  it("matches a schema by file name when the files came without folders", () => {
    const result = rowsFromFiles([
      { path: "agents.toml", text: REGISTRY },
      { path: "BriefedAgent.schema.json", text: JSON.stringify(SCHEMA) }
    ]);

    assert.ok("rows" in result);
    assert.deepEqual(JSON.parse(result.rows[0].schema), SCHEMA);
  });

  it("says which schema the chosen files do not settle", () => {
    const missing = rowsFromFiles([{ path: "agents.toml", text: REGISTRY }]);
    const twice = rowsFromFiles([
      { path: "agents.toml", text: REGISTRY },
      { path: "a/BriefedAgent.schema.json", text: "{}" },
      { path: "b/BriefedAgent.schema.json", text: "{}" }
    ]);

    assert.ok("rows" in missing && "rows" in twice);
    assert.match(missing.problems[0].schema, /not among the chosen files/);
    assert.match(twice.problems[0].schema, /2 chosen files are named/);
  });

  it("refuses what is not one agents.toml with agents in it", () => {
    assert.match(rowsFromFiles([]).error, /Choose an agents.toml/);
    assert.match(rowsFromFiles([{ path: "a.toml", text: REGISTRY }, { path: "b.toml", text: REGISTRY }]).error, /not 2/);
    assert.match(rowsFromFiles([{ path: "agents.toml", text: "agents = [" }]).error, /not TOML/);
    assert.match(rowsFromFiles([{ path: "agents.toml", text: "title = 'x'" }]).error, /no \[\[agents\]\]/);
  });
});

describe("registryArchive", () => {
  const registrations = [
    { key: "briefed", workflow_type: "BriefedAgent", task_queue: "briefed-queue", label: "Briefed", description: "Takes a brief.", schema: SCHEMA },
    { key: "plain", workflow_type: "Plain/Agent", task_queue: "plain-queue", label: "Plain", description: "Takes nothing.", schema: null }
  ];

  it("zips an agents.toml naming one schema file per agent that has one", () => {
    const files = filesFromZip(registryArchive(registrations));

    assert.deepEqual(files.map((file) => file.path).sort(), ["agents/BriefedAgent.schema.json", "agents/agents.toml"]);
    const toml = parse(files.find((file) => file.path === "agents/agents.toml").text);
    assert.equal(toml.agents[0].schema, "BriefedAgent.schema.json");
    assert.equal(toml.agents[1].schema, undefined);
  });

  it("imports back into the registrations it was made from", () => {
    const imported = rowsFromFiles(filesFromZip(registryArchive(registrations)));

    assert.ok("rows" in imported);
    const checked = checkRows(imported.rows);
    assert.ok("registrations" in checked);
    assert.deepEqual(checked.registrations, registrations);
  });

  it("gives two workflow types that write the same file name a file each", () => {
    const clash = [
      { ...registrations[0], key: "a", workflow_type: "A/B", schema: { agent: "A/B" } },
      { ...registrations[0], key: "b", workflow_type: "A_B", schema: { agent: "A_B" } }
    ];

    const names = filesFromZip(registryArchive(clash)).map((file) => file.path).sort();

    assert.deepEqual(names, ["agents/A_B-2.schema.json", "agents/A_B.schema.json", "agents/agents.toml"]);
  });
});

describe("rowFromRegistration", () => {
  it("writes the schema as JSON text the form can edit", () => {
    const registration = { key: "briefed", workflow_type: "BriefedAgent", task_queue: "q", label: "B", description: "D", schema: SCHEMA };

    assert.deepEqual(JSON.parse(rowFromRegistration(registration).schema), SCHEMA);
    assert.equal(rowFromRegistration({ ...registration, schema: null }).schema, "");
  });
});
