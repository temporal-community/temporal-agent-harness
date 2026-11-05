#!/usr/bin/env node
// Node-only development CLI. Resources come from the installed core package, not a checkout.
import { createHash } from "node:crypto";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  unlinkSync,
  writeFileSync,
  realpathSync,
  statSync,
} from "node:fs";
import { createRequire } from "node:module";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const NAME = "temporal-harness-typescript";
const MANIFEST = ".harness-skill.json";
// Where each coding agent discovers project skills; `skills install --for <tool>` targets one.
export const SKILL_DIRECTORIES = {
  codex: ".agents/skills",
  claude: ".claude/skills",
};
const DEFAULT_SKILL_DIRECTORY = SKILL_DIRECTORIES.codex;
// How each tool invokes the installed skill from a prompt.
const SKILL_PROMPTS = { Codex: `$${NAME}`, "Claude Code": `/${NAME}` };
export function skillDestinations(tools = [], dest) {
  const unknown = tools.filter((tool) => !(tool in SKILL_DIRECTORIES));
  if (unknown.length)
    throw new Error(
      `Unknown coding agent ${unknown.join(", ")}; choose from ${Object.keys(SKILL_DIRECTORIES).join(", ")}`,
    );
  const destinations = tools.map((tool) => SKILL_DIRECTORIES[tool]);
  if (dest !== undefined) destinations.push(dest);
  return [...new Set(destinations.length ? destinations : [DEFAULT_SKILL_DIRECTORY])];
}
/** A package-manager-safe name derived from the starter directory, e.g. `My Agent` -> `my-agent`. */
export function projectName(destination) {
  const slug = basename(resolve(destination))
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "my-agent";
}
const hash = (bytes) => createHash("sha256").update(bytes).digest("hex");
const present = (path) => {
  try {
    return lstatSync(path);
  } catch (e) {
    if (e.code === "ENOENT") return null;
    throw e;
  }
};
function target(root, name) {
  if (
    !name ||
    isAbsolute(name) ||
    name.split(/[\\/]/).some((p) => p === ".." || p === ".")
  )
    throw new Error(`Invalid resource path: ${name}`);
  const path = join(root, name);
  for (let current = path; ; current = dirname(current)) {
    const stat = present(current);
    if (stat?.isSymbolicLink())
      throw new Error(`Refusing to write through a symlink: ${current}`);
    if (stat && current !== path && !stat.isDirectory())
      throw new Error(`Not a directory: ${current}`);
    if (current === root) break;
  }
  return path;
}
export function resourceFiles(root, directory = root) {
  const files = new Map();
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (statSync(path).isDirectory())
      for (const [name, data] of resourceFiles(root, path))
        files.set(name, data);
    else if (
      statSync(path).isFile() &&
      /\.(md|yaml|json|tmpl)$/.test(entry.name)
    )
      files.set(relative(root, path).split(sep).join("/"), readFileSync(path));
  }
  return files;
}
export function installSkill(source, destination, version) {
  const root = resolve(destination, NAME);
  const files = resourceFiles(source);
  if (!files.has("SKILL.md"))
    throw new Error("The installed package is missing its SKILL.md");
  const marker = target(root, MANIFEST);
  let previous = {};
  if (existsSync(marker)) {
    const metadata = JSON.parse(readFileSync(marker, "utf8"));
    if (
      !metadata ||
      metadata.name !== NAME ||
      !metadata.files ||
      typeof metadata.files !== "object" ||
      Array.isArray(metadata.files)
    )
      throw new Error("Invalid skill ownership manifest");
    previous = metadata.files;
    if (
      Object.hasOwn(previous, MANIFEST) ||
      Object.values(previous).some((value) => typeof value !== "string")
    )
      throw new Error("Invalid skill ownership manifest");
  }
  const conflicts = [];
  for (const name of new Set([...files.keys(), ...Object.keys(previous)])) {
    const path = target(root, name);
    const stat = present(path);
    if (!stat) continue;
    if (!stat.isFile()) {
      conflicts.push(name);
      continue;
    }
    const data = readFileSync(path);
    if (
      !(files.has(name) && data.equals(files.get(name))) &&
      hash(data) !== previous[name]
    )
      conflicts.push(name);
  }
  if (conflicts.length)
    throw new Error(
      `Local skill edits preserved; resolve conflicts or choose --dest: ${conflicts.sort().join(", ")}`,
    );
  const stale = [...files].filter(([name, data]) => {
    const stat = present(target(root, name));
    return !stat?.isFile() || !readFileSync(target(root, name)).equals(data);
  });
  const obsolete = Object.keys(previous).filter(
    (name) => !files.has(name) && existsSync(target(root, name)),
  );
  for (const [name, data] of stale) {
    const path = target(root, name);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, data);
  }
  for (const name of obsolete) unlinkSync(target(root, name));
  mkdirSync(root, { recursive: true });
  writeFileSync(
    marker,
    JSON.stringify(
      {
        name: NAME,
        version,
        files: Object.fromEntries(
          [...files].map(([name, data]) => [name, hash(data)]),
        ),
      },
      null,
      2,
    ) + "\n",
  );
  return { root, changed: stale.length > 0 || obsolete.length > 0 };
}
export function createStarter(source, destination, version) {
  const root = resolve(destination);
  const project = projectName(root);
  const files = [...resourceFiles(source)]
    .filter(([name]) => name.endsWith(".tmpl"))
    .map(([name, data]) => [
      name === "gitignore.tmpl" ? ".gitignore" : name.slice(0, -5),
      data
        .toString()
        .replaceAll("__HARNESS_VERSION__", version)
        .replaceAll("__PROJECT_NAME__", project),
    ]);
  if (!files.length)
    throw new Error("The installed package is missing its starter");
  for (const [name] of files)
    if (present(target(root, name)))
      throw new Error(
        `Starter would overwrite ${join(root, name)}; choose a new directory`,
      );
  for (const [name, data] of files) {
    const path = target(root, name);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, data, { flag: "wx" });
  }
  return root;
}
export async function diagnose({
  apiUrl = "http://localhost:8000",
  schema,
  timeout = 5000,
  version = "unknown",
} = {}) {
  if (!Number.isFinite(timeout) || timeout <= 0)
    throw new Error("timeout must be positive");
  const url = new URL(apiUrl);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password
  )
    throw new Error("API URL must be http(s) without embedded credentials");
  const checks = [];
  if (schema) {
    try {
      const document = JSON.parse(readFileSync(schema, "utf8"));
      if (
        typeof document.agent !== "string" ||
        !document.agent ||
        !document.handlers ||
        typeof document.handlers !== "object" ||
        Array.isArray(document.handlers)
      )
        throw new Error(
          "Expected an agent schema generated by agent-harness-schema",
        );
      checks.push({ check: "schema", ok: true, detail: document.agent });
    } catch (error) {
      checks.push({ check: "schema", ok: false, detail: error.message });
    }
  }
  try {
    const response = await fetch(apiUrl.replace(/\/$/, "") + "/api/agents", {
      signal: AbortSignal.timeout(timeout),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json();
    if (!Array.isArray(body.agents))
      throw new Error("Invalid agent registry response");
    checks.push({
      check: "api",
      ok: true,
      detail: `${body.agents.length} registered agents`,
    });
    for (const agent of body.agents)
      checks.push({
        check: `worker:${agent.workflow_type}`,
        ok: agent.worker?.status === "ready",
        detail: agent.worker?.status ?? "availability not reported",
      });
  } catch (error) {
    checks.push({
      check: "api",
      ok: false,
      detail: `${error.name}: check the API and session-manager terminals`,
    });
  }
  return {
    ok: checks.every((c) => c.ok),
    versions: {
      "@temporalio/agent-harness": version,
      node: process.versions.node,
    },
    checks,
  };
}
const USAGE = `usage: agent-harness init <directory>
       agent-harness schema ./dist/agents.js#Export [-o Agent.schema.json]
       agent-harness skills path
       agent-harness skills install [--for codex|claude]... [--dest <directory>]
       agent-harness doctor [--schema Agent.schema.json] [--api-url http://localhost:8000] [--timeout 5] [--json]

Uses this installed SDK release's resources. \`schema\` is \`agent-harness-schema\`.
\`skills install\` copies the skill where your coding agent discovers it: Codex reads
.agents/skills (the default), Claude Code reads .claude/skills. Reruns preserve local edits.
Doctor checks the schema and shared API/worker availability without creating sessions.
Use the Python CLI's doctor --registry for direct Temporal and TOML checks.`;
export async function main(args = process.argv.slice(2)) {
  if (args[0] === "schema") {
    // One CLI for the starter: delegate to the schema command this package already ships.
    const { main: schemaMain } = await import(
      new URL("../dist/src/harness/agent-schema.js", import.meta.url)
    );
    await schemaMain(args.slice(1));
    return;
  }
  const { values, positionals } = parseArgs({
    args,
    allowPositionals: true,
    options: {
      for: { type: "string", multiple: true },
      dest: { type: "string" },
      schema: { type: "string" },
      "api-url": { type: "string" },
      timeout: { type: "string" },
      json: { type: "boolean" },
      help: { type: "boolean", short: "h" },
    },
  });
  if (values.help) {
    console.log(USAGE);
    return;
  }
  const [command, action] = positionals;
  if (
    positionals.length > 2 ||
    !["init", "skills", "doctor"].includes(command) ||
    (command === "init" && !action) ||
    (command === "skills" && !["path", "install"].includes(action)) ||
    (command === "doctor" && action)
  )
    throw new Error(USAGE);
  const require = createRequire(import.meta.url);
  const source = dirname(require.resolve("@temporalio/agent-harness/skill"));
  const { version } = JSON.parse(
    readFileSync(join(source, "../../package.json"), "utf8"),
  );
  if (command === "skills" && action === "path") console.log(source);
  else if (command === "skills") {
    for (const destination of skillDestinations(values.for, values.dest)) {
      const { root, changed } = installSkill(source, destination, version);
      console.log(
        `${changed ? "Installed" : "Already up to date:"} ${NAME} ${version} in ${root}`,
      );
    }
    const prompts = Object.entries(SKILL_PROMPTS)
      .map(([tool, prompt]) => `\`${prompt}\` in ${tool}`)
      .join(", ");
    console.log(`Next: ask your coding agent to use the skill (${prompts}).`);
  } else if (command === "init") {
    const root = createStarter(join(source, "assets/starter"), action, version);
    console.log(`Created ${root}`);
    console.log(
      "Next: follow its README.md to install dependencies, add credentials, and start the services.",
    );
  } else {
    const report = await diagnose({
      apiUrl: values["api-url"],
      schema: values.schema,
      timeout:
        values.timeout === undefined ? 5000 : Number(values.timeout) * 1000,
      version,
    });
    if (values.json) console.log(JSON.stringify(report, null, 2));
    else {
      console.log(
        `Versions: harness=${version}, node=${process.versions.node}`,
      );
      for (const c of report.checks)
        console.log(`${c.ok ? "OK" : "FAIL"} ${c.check}: ${c.detail}`);
    }
    if (!report.ok) process.exitCode = 1;
  }
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href
) {
  main().catch((error) => {
    console.error(`agent-harness: ${error.message}`);
    process.exitCode = 1;
  });
}
