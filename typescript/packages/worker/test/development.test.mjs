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
  chooseSkillTools,
  createStarter,
  installSkill,
  diagnose,
  projectName,
  skillDestinations,
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
  const { root, changed } = installSkill(source, dest, "1");
  assert.equal(changed, true);
  assert.equal(installSkill(source, dest, "1").changed, false);
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
  assert.equal(installSkill(source, dest, "2").changed, true);
  assert.equal(readFileSync(join(root, "SKILL.md"), "utf8"), "second");
  assert.equal(existsSync(join(root, "old.md")), false);
  assert.equal(readFileSync(join(root, "custom.md"), "utf8"), "mine");
});
test("refuses symlinks and path traversal in ownership manifests", (t) => {
  const { dir, source } = fixture(t);
  const dest = join(dir, "skills");
  const { root } = installSkill(source, dest, "1");
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
test("skill destinations map tools to their directories", () => {
  assert.deepEqual(skillDestinations(["claude", "agents", "claude"]), [
    ".claude/skills",
    ".agents/skills",
  ]);
  assert.deepEqual(skillDestinations(["claude"], "tools/skills"), [
    ".claude/skills",
    "tools/skills",
  ]);
  assert.throws(() => skillDestinations(), /--for \{claude,agents\} or --dest/);
  assert.throws(() => skillDestinations(["cursor"]), /Unknown coding agent cursor/);
});
test("choosing skill tools re-asks until it reads valid numbers", async () => {
  const answers = ["", "3", "2, 1 2"];
  const questions = [];
  const tools = await chooseSkillTools(async (question) => {
    questions.push(question);
    return answers.shift();
  });
  assert.deepEqual(tools, ["agents", "claude"]);
  assert.equal(questions.length, 3);
  assert.match(questions[0], /1\) Claude Code: \.claude\/skills/);
  assert.match(
    questions[0],
    /2\) Codex and other agents that read the shared \.agents\/skills: \.agents\/skills/,
  );
});
test("project name comes from the directory", () => {
  assert.equal(projectName("/tmp/My Agent.Project"), "my-agent-project");
  assert.equal(projectName("/tmp/---"), "my-agent");
});
test("starter pins installed SDK version and refuses existing files", (t) => {
  const { dir, source } = fixture(t);
  writeFileSync(
    join(source, "package.json.tmpl"),
    '{"name":"__PROJECT_NAME__","version":"__HARNESS_VERSION__"}',
  );
  writeFileSync(join(source, "gitignore.tmpl"), ".env.local\n");
  const root = createStarter(source, join(dir, "My App"), "0.9.0");
  const manifest = JSON.parse(readFileSync(join(root, "package.json")));
  assert.equal(manifest.version, "0.9.0");
  assert.equal(manifest.name, "my-app");
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
