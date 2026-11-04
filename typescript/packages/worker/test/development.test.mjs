import assert from "node:assert/strict";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  rmSync,
  unlinkSync,
  symlinkSync,
  existsSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";
import { test } from "node:test";
import {
  createStarter,
  installSkill,
  diagnose,
} from "../bin/agent-harness.mjs";

function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), "harness-skill-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const source = join(dir, "source");
  mkdirSync(source);
  writeFileSync(join(source, "SKILL.md"), "first");
  writeFileSync(join(source, "old.md"), "owned");
  return { dir, source };
}
test("updates owned files while preserving local edits and unrelated files", (t) => {
  const { dir, source } = fixture(t);
  const dest = join(dir, "skills");
  const root = installSkill(source, dest, "1");
  writeFileSync(join(root, "old.md"), "my edit");
  writeFileSync(join(source, "SKILL.md"), "second");
  assert.throws(
    () => installSkill(source, dest, "2"),
    /Local skill edits preserved/,
  );
  assert.equal(readFileSync(join(root, "SKILL.md"), "utf8"), "first");
  writeFileSync(join(root, "old.md"), "owned");
  unlinkSync(join(source, "old.md"));
  writeFileSync(join(root, "custom.md"), "mine");
  installSkill(source, dest, "2");
  assert.equal(readFileSync(join(root, "SKILL.md"), "utf8"), "second");
  assert.equal(existsSync(join(root, "old.md")), false);
  assert.equal(readFileSync(join(root, "custom.md"), "utf8"), "mine");
});
test("refuses symlinks and path traversal in ownership manifests", (t) => {
  const { dir, source } = fixture(t);
  const dest = join(dir, "skills");
  const root = installSkill(source, dest, "1");
  const outside = join(dir, "outside");
  writeFileSync(outside, "mine");
  unlinkSync(join(root, "old.md"));
  symlinkSync(outside, join(root, "old.md"));
  assert.throws(() => installSkill(source, dest, "2"), /symlink/);
  assert.equal(readFileSync(outside, "utf8"), "mine");
  unlinkSync(join(root, "old.md"));
  writeFileSync(
    join(root, ".harness-skill.json"),
    JSON.stringify({
      name: "temporal-harness-typescript",
      files: { "../../outside": "x" },
    }),
  );
  assert.throws(() => installSkill(source, dest, "2"), /Invalid resource path/);
});
test("starter pins installed SDK version and refuses existing files", (t) => {
  const { dir, source } = fixture(t);
  writeFileSync(
    join(source, "package.json.tmpl"),
    '{"version":"__HARNESS_VERSION__"}',
  );
  writeFileSync(join(source, "gitignore.tmpl"), ".env.local\n");
  const root = createStarter(source, join(dir, "app"), "0.9.0");
  assert.equal(
    JSON.parse(readFileSync(join(root, "package.json"))).version,
    "0.9.0",
  );
  assert.ok(existsSync(join(root, ".gitignore")));
  assert.throws(() => createStarter(source, root, "1.0.0"), /overwrite/);
});
test("doctor reports missing workers and invalid schemas without writing to the API", async (t) => {
  const { dir } = fixture(t);
  const methods = [];
  const server = createServer((req, res) => {
    methods.push(req.method);
    res.setHeader("Content-Type", "application/json");
    res.end(
      JSON.stringify({
        agents: [{ workflow_type: "Hello", worker: { status: "no_worker" } }],
      }),
    );
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());
  const schema = join(dir, "agent.json");
  writeFileSync(schema, JSON.stringify({ agent: "Hello", handlers: {} }));
  const result = await diagnose({
    apiUrl: `http://127.0.0.1:${server.address().port}`,
    schema,
  });
  assert.equal(result.ok, false);
  assert.equal(result.checks[0].ok, true);
  assert.equal(result.checks.at(-1).detail, "no_worker");
  assert.deepEqual(methods, ["GET"]);
  writeFileSync(schema, "{}");
  assert.equal(
    (
      await diagnose({
        apiUrl: `http://127.0.0.1:${server.address().port}`,
        schema,
      })
    ).checks[0].ok,
    false,
  );
});
