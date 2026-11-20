/**
 * Registering agents from the console: the form's rows and their checks, reading an
 * `agents.toml` and the schema files it names (chosen one by one, as a folder, or as a zip the
 * console downloaded), and writing the zip a download saves. The checks are the server's
 * `agents.toml` rules, so a bad entry is refused before it is sent; the server checks again.
 */
import { strFromU8, strToU8, unzipSync, zipSync } from "fflate";
import { parse, stringify } from "smol-toml";
import type { AgentRegistration, JsonRecord } from "$lib/api/types";

/** What the console needs to change the server's registry. */
export interface AgentRegistryActions {
  /** Register every agent, or none. */
  register(agents: AgentRegistration[]): Promise<void>;
  /** The agent as it was registered. */
  registration(key: string): Promise<AgentRegistration>;
  retire(key: string): Promise<void>;
  remove(key: string): Promise<void>;
}

/** One agent in the form. `schema` is the schema document's JSON text, empty for none. */
export interface RegistrationRow {
  key: string;
  workflow_type: string;
  task_queue: string;
  label: string;
  description: string;
  schema: string;
}

export type RowField = keyof RegistrationRow;
export type RowProblems = Partial<Record<RowField, string>>;

export const REQUIRED_FIELDS = ["key", "workflow_type", "task_queue", "label", "description"] as const;

export function emptyRow(): RegistrationRow {
  return { key: "", workflow_type: "", task_queue: "", label: "", description: "", schema: "" };
}

export function rowFromRegistration(registration: AgentRegistration): RegistrationRow {
  return {
    key: registration.key,
    workflow_type: registration.workflow_type,
    task_queue: registration.task_queue,
    label: registration.label,
    description: registration.description,
    schema: registration.schema ? JSON.stringify(registration.schema, null, 2) : ""
  };
}

/**
 * The registrations the rows describe, or each row's problems. A key or workflow type given
 * twice is a problem on its later row, so the first stays as written.
 */
export function checkRows(rows: RegistrationRow[]): { registrations: AgentRegistration[] } | { problems: RowProblems[] } {
  const problems: RowProblems[] = rows.map(() => ({}));
  const keys = new Map<string, number>();
  const types = new Map<string, number>();
  const registrations: AgentRegistration[] = [];

  rows.forEach((row, index) => {
    const found = problems[index];
    for (const field of REQUIRED_FIELDS) {
      if (!row[field].trim()) found[field] = "Required.";
    }
    const key = row.key.trim();
    const type = row.workflow_type.trim();
    if (key && keys.has(key)) found.key = `Also the key of row ${keys.get(key)! + 1}.`;
    if (key && !keys.has(key)) keys.set(key, index);
    if (type && types.has(type)) found.workflow_type = `Also the workflow type of row ${types.get(type)! + 1}.`;
    if (type && !types.has(type)) types.set(type, index);

    let schema: JsonRecord | null = null;
    if (row.schema.trim()) {
      const parsed = schemaDocument(row.schema, type);
      if ("problem" in parsed) found.schema = parsed.problem;
      else schema = parsed.document;
    }
    registrations.push({
      key,
      workflow_type: type,
      task_queue: row.task_queue.trim(),
      label: row.label.trim(),
      description: row.description.trim(),
      schema
    });
  });

  return problems.some((found) => Object.keys(found).length > 0) ? { problems } : { registrations };
}

function schemaDocument(text: string, workflowType: string): { document: JsonRecord } | { problem: string } {
  let document: unknown;
  try {
    document = JSON.parse(text);
  } catch (error) {
    return { problem: `Not JSON: ${error instanceof Error ? error.message : String(error)}` };
  }
  if (typeof document !== "object" || document === null || Array.isArray(document)) {
    return { problem: "A schema document is a JSON object." };
  }
  const agent = (document as JsonRecord).agent;
  if (workflowType && agent !== workflowType) {
    return { problem: `Describes the workflow ${JSON.stringify(agent ?? null)}, not ${JSON.stringify(workflowType)}.` };
  }
  return { document: document as JsonRecord };
}

/** A chosen file: its path within what was chosen (a folder's relative path, else its name). */
export interface ChosenFile {
  path: string;
  text: string;
}

export interface ImportedRows {
  rows: RegistrationRow[];
  /** One per row: a `schema` the chosen files do not settle. */
  problems: RowProblems[];
}

/**
 * The rows an `agents.toml` among `files` describes, each with the schema document its
 * `schema` names. A `schema` path is resolved against the TOML file's folder when the files
 * came with paths, and otherwise matched by file name. Keys the format does not name are
 * ignored, as the server ignores them.
 */
export function rowsFromFiles(files: ChosenFile[]): ImportedRows | { error: string } {
  const registries = files.filter((file) => file.path.toLowerCase().endsWith(".toml"));
  if (registries.length === 0) return { error: "Choose an agents.toml." };
  if (registries.length > 1) {
    return { error: `Choose one agents.toml, not ${registries.length} (${registries.map((file) => file.path).join(", ")}).` };
  }
  const [registry] = registries;
  let raw: unknown;
  try {
    raw = parse(registry.text);
  } catch (error) {
    return { error: `${registry.path} is not TOML: ${error instanceof Error ? error.message : String(error)}` };
  }
  const entries = (raw as { agents?: unknown }).agents;
  if (!Array.isArray(entries) || entries.length === 0) {
    return { error: `${registry.path} has no [[agents]] entries.` };
  }

  const folder = dirname(registry.path);
  const rows: RegistrationRow[] = [];
  const problems: RowProblems[] = [];
  for (const entry of entries) {
    const fields = typeof entry === "object" && entry !== null ? (entry as Record<string, unknown>) : {};
    const text = (name: string) => (typeof fields[name] === "string" ? (fields[name] as string) : "");
    const row: RegistrationRow = {
      key: text("key"),
      workflow_type: text("workflow_type"),
      task_queue: text("task_queue"),
      label: text("label"),
      description: text("description").split(/\s+/).filter(Boolean).join(" "),
      schema: ""
    };
    const found: RowProblems = {};
    const schemaPath = text("schema");
    if (schemaPath) {
      const resolved = schemaFile(files, folder, schemaPath);
      if ("problem" in resolved) found.schema = resolved.problem;
      else row.schema = resolved.file.text;
    }
    rows.push(row);
    problems.push(found);
  }
  return { rows, problems };
}

function schemaFile(files: ChosenFile[], folder: string, schemaPath: string): { file: ChosenFile } | { problem: string } {
  const wanted = joinPath(folder, schemaPath);
  const exact = files.find((file) => normalizePath(file.path) === wanted);
  if (exact) return { file: exact };
  const name = basename(schemaPath);
  const named = files.filter((file) => basename(file.path) === name);
  if (named.length === 1) return { file: named[0] };
  if (named.length > 1) return { problem: `${named.length} chosen files are named ${name}; choose the folder instead.` };
  return { problem: `${schemaPath} is not among the chosen files.` };
}

/** Every file in a zip, as text, by its path inside it. */
export function filesFromZip(bytes: Uint8Array): ChosenFile[] {
  return Object.entries(unzipSync(bytes))
    .filter(([path]) => !path.endsWith("/"))
    .map(([path, data]) => ({ path, text: strFromU8(data) }));
}

export const ARCHIVE_FOLDER = "agents";

/**
 * A zip of an `agents.toml` registering `registrations` and the schema files it names, inside
 * one `agents/` folder: what `serve`, `temporal-agent-kit agents add` and the console's import
 * take. Each schema file is named after its agent's workflow type.
 */
export function registryArchive(registrations: AgentRegistration[]): Uint8Array {
  const files: Record<string, Uint8Array> = {};
  const used = new Set<string>();
  const entries = registrations.map((registration) => {
    const entry: Record<string, string> = {
      key: registration.key,
      workflow_type: registration.workflow_type,
      task_queue: registration.task_queue,
      label: registration.label
    };
    if (registration.schema) {
      const name = uniqueName(`${safeName(registration.workflow_type)}.schema.json`, used);
      entry.schema = name;
      files[`${ARCHIVE_FOLDER}/${name}`] = strToU8(`${JSON.stringify(registration.schema, null, 2)}\n`);
    }
    entry.description = registration.description;
    return entry;
  });
  files[`${ARCHIVE_FOLDER}/agents.toml`] = strToU8(`${stringify({ agents: entries })}\n`);
  return zipSync(files);
}

function safeName(name: string): string {
  return name.replace(/[^A-Za-z0-9._-]/g, "_") || "agent";
}

function uniqueName(name: string, used: Set<string>): string {
  let candidate = name;
  for (let n = 2; used.has(candidate); n++) candidate = name.replace(/\.schema\.json$/, `-${n}.schema.json`);
  used.add(candidate);
  return candidate;
}

function normalizePath(path: string): string {
  const parts: string[] = [];
  for (const part of path.replace(/\\/g, "/").split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") parts.pop();
    else parts.push(part);
  }
  return parts.join("/");
}

function joinPath(folder: string, path: string): string {
  return normalizePath(folder ? `${folder}/${path}` : path);
}

function dirname(path: string): string {
  const normal = normalizePath(path);
  const slash = normal.lastIndexOf("/");
  return slash === -1 ? "" : normal.slice(0, slash);
}

function basename(path: string): string {
  const normal = normalizePath(path);
  return normal.slice(normal.lastIndexOf("/") + 1);
}
