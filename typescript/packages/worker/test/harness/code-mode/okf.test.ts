import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { ApplicationFailure } from "@temporalio/activity";
import {
  FsError,
  InMemoryFileSystem,
  Mount,
  StateRef,
  activityFilesystem,
  activityName,
  indexTrack,
  okfBundleVfsMount,
  okfCodeModeTool,
  vfsMount,
  type FileIndex,
} from "@temporalio/agent-harness";
import {
  linkTargets,
  renderConcept,
  splitFrontmatter,
} from "@temporalio/agent-harness/harness/code-mode/okf";
import { parse } from "yaml";
import { z } from "zod";
import { AgentHarnessPlugin } from "../../../src/plugin.ts";
import { ConfigError, filesystemActivities } from "../../../src/harness/code-mode/activity-fs.ts";
import { buildOkfGraph, parseConcept } from "../../../src/harness/code-mode/okf.ts";
import type { OkfReadable } from "../../../src/harness/code-mode/okf.ts";
import { assertMatch } from "../../match.ts";

const NOW = new Date("2026-10-06T00:00:00Z");

function concept(fields: string, body = ""): string {
  return `---\n${fields}\n---\n\n${body}`;
}

function parsed(text: string, path = "a.md") {
  return parseConcept(path.replace(/\.md$/, ""), path, text, text.length, NOW).concept;
}

class Bundle implements OkfReadable {
  readonly files: Record<string, Uint8Array>;
  readonly reads: [string, number | null | undefined][] = [];
  constructor(files: Record<string, string | Uint8Array>) {
    this.files = Object.fromEntries(
      Object.entries(files).map(([key, value]) => [
        key,
        typeof value === "string" ? new TextEncoder().encode(value) : value,
      ]),
    );
  }
  dirs(): Set<string> {
    const out = new Set(["."]);
    for (const key of Object.keys(this.files)) {
      const parts = key.split("/");
      for (let i = 1; i < parts.length; i++) out.add(parts.slice(0, i).join("/"));
    }
    return out;
  }
  async stat(path: string) {
    if (this.dirs().has(path)) return { is_dir: true, size: 0 };
    const data = this.files[path];
    return data === undefined ? null : { is_dir: false, size: data.length };
  }
  async read(path: string, offset = 0, length: number | null = null) {
    this.reads.push([path, length]);
    const data = this.files[path];
    if (data === undefined)
      throw new FsError("FileNotFoundError", 2, "No such file or directory", path);
    const end = length === null ? data.length : offset + length;
    return data.subarray(offset, end);
  }
  async list(path: string) {
    const prefix = path === "." ? "" : `${path}/`;
    return [
      ...new Set(
        Object.keys(this.files)
          .filter((key) => key.startsWith(prefix))
          .map((key) => key.slice(prefix.length).split("/")[0]!),
      ),
    ].sort();
  }
}

describe("a concept", () => {
  it("reads its frontmatter", () => {
    const found = parsed(
      concept(
        "type: Preference\ntitle: Coffee\ndescription: A flat white.\ntags: [drinks, morning]\nstatus: draft",
      ),
      "prefs/coffee.md",
    );
    assertMatch(found, {
      id: "prefs/coffee",
      path: "prefs/coffee.md",
      type: "Preference",
      title: "Coffee",
      description: "A flat white.",
      tags: ["drinks", "morning"],
      status: "draft",
      problem: null,
    });
  });

  for (const [text, problem] of [
    ["# Just a heading\n", "no frontmatter"],
    ["---\ntype: Fact\n", "never closed"],
    ["---\ntype: [unclosed\n---\n", "not valid YAML"],
    ["---\n- a\n- b\n---\n", "not a mapping"],
    ["---\n---\n", "empty"],
    ["---\nnote: no type here\n---\n", "no `type`"],
  ] as const)
    it(`keeps a broken concept and says why: ${problem}`, () => {
      const found = parsed(text, "notes/odd.md");
      assert.equal(found.type, "Unknown");
      assert.equal(found.title, "odd");
      assert.ok(found.problem?.includes(problem));
    });

  it("lists tags once and reads trust and staleness", () => {
    assert.deepStrictEqual(parsed(concept("type: Fact\ntags: [a, b, a, '', b]")).tags, ["a", "b"]);
    assert.deepStrictEqual(parsed(concept("type: Fact\ntags: a, a")).tags, ["a"]);
    assert.equal(
      parsed(concept("type: Fact\nverified: {by: 'human:user', at: 2026-01-01T00:00:00Z}"))
        .trust_tier,
      "human-reviewed",
    );
    assert.equal(parsed(concept("type: Fact\n")).trust_tier, "unverified");
    assert.equal(parsed(concept("type: Plan\nstale_after: 2026-10-06T00:00:00Z")).stale, true);
    assert.equal(parsed(concept("type: Plan\nstale_after: 2026-12-31")).stale, false);
    assert.equal(parsed(concept("type: Plan\nstale_after: not a date")).stale, false);
  });
});

describe("links", () => {
  it("resolves relative to the file or the bundle root", () => {
    const body = [
      "See [Ana](../people/ana.md), [the trip](denver.md#dates) and [x](/projects/x.md).",
      "Again [Ana](../people/ana.md) and [pic](photo.png) and [web](https://e.com/a.md).",
      "Also [mail](mailto:a@b.md), [up and out](../../outside.md), [index](index.md).",
      "And [me](trip.md) and [spaced](<my%20notes.md>).",
    ].join("\n");
    assert.deepStrictEqual(linkTargets(body, "trips/trip.md"), [
      "people/ana",
      "trips/denver",
      "projects/x",
      "trips/my notes",
    ]);
  });

  it("ignores links inside code fences", () => {
    const body =
      "```\n[no](a.md)\n```\n[yes](b.md)\n~~~python\n[no](c.md)\n```\nstill fenced\n~~~\n";
    assert.deepStrictEqual(linkTargets(body, "x.md"), ["b"]);
  });
});

describe("a walk", () => {
  it("lists concepts and the links between them", async () => {
    const fs = new Bundle({
      "index.md": "# Memory\n* [Coffee](prefs/coffee.md)\n",
      "log.md": "# Log\n",
      "prefs/coffee.md": concept("type: Preference", "Goes with [Ana](../people/ana.md)."),
      "people/ana.md": concept(
        "type: Person",
        "Likes [coffee](../prefs/coffee.md), [tea](../prefs/tea.md).",
      ),
      "people/index.md": "* [Ana](ana.md)\n",
      "scratch.txt": "not markdown",
      "broken.md": "no frontmatter at all",
    });
    const graph = await buildOkfGraph(fs, { prefix: ".", links: true, now: NOW });
    assert.deepStrictEqual(
      graph.concepts.map((item) => item.id),
      ["broken", "people/ana", "prefs/coffee"],
    );
    assert.equal(graph.concepts[0]?.type, "Unknown");
    assert.ok(graph.concepts[0]?.problem);
    assert.deepStrictEqual(
      graph.links.map((link) => [link.source, link.target, link.dangling]),
      [
        ["people/ana", "prefs/coffee", false],
        ["people/ana", "prefs/tea", true],
        ["prefs/coffee", "people/ana", false],
      ],
    );
    assert.equal(graph.truncated, false);
    assert.equal(graph.walked_at, NOW.getTime() / 1000);
  });

  it("reads only each file's head when links are not wanted", async () => {
    const fs = new Bundle({
      "big.md": concept("type: Fact", "x".repeat(50_000)),
      "long.md": `---\ntype: Fact\nnote: ${"y".repeat(10_000)}\n---\nbody\n`,
    });
    const graph = await buildOkfGraph(fs, { prefix: ".", links: false, now: NOW });
    assert.deepStrictEqual(
      graph.concepts.map((item) => item.type),
      ["Fact", "Fact"],
    );
    assert.deepStrictEqual(graph.links, []);
    assert.deepStrictEqual(fs.reads, [
      ["big.md", 8192],
      ["long.md", 8192],
      ["long.md", 256_000],
    ]);
  });

  it("stops at its limits and lists an unreadable concept", async () => {
    const fs = new Bundle(
      Object.fromEntries([...Array(4)].map((_, i) => [`c${i}.md`, concept("type: Fact")])),
    );
    const capped = await buildOkfGraph(fs, {
      prefix: ".",
      links: true,
      now: NOW,
      limits: { concepts: 2 },
    });
    assert.deepStrictEqual(
      capped.concepts.map((item) => item.id),
      ["c0", "c1"],
    );
    assert.equal(capped.truncated, true);
    const flaky = new Bundle({ "a.md": "x" });
    flaky.read = async (path) => {
      throw new FsError("PermissionError", 13, "Permission denied", path);
    };
    const graph = await buildOkfGraph(flaky, { prefix: ".", links: true, now: NOW });
    assert.equal(graph.concepts[0]?.problem, "unreadable: Permission denied");
  });
});

describe("render", () => {
  it("writes frontmatter, records who generated it, and stamps verified", () => {
    const text = renderConcept(
      { type: "Plan", title: "Denver", tags: ["travel"], stale_after: "2026-10-15" },
      "\n\nFlying on the 14th.",
      { by: "CodeModeMemoryAgent", at: new Date("2026-10-06T12:30:05.123Z") },
    );
    assert.equal(text.startsWith("---\ntype: Plan\ntitle: Denver\n"), true);
    assert.equal(text.endsWith("---\n\nFlying on the 14th.\n"), true);
    assert.ok(text.includes("generated:\n  by: CodeModeMemoryAgent\n"));
    const fields = parse(text.split("---")[1]!, { schema: "yaml-1.1" }) as {
      generated: { by: string; at: string };
    };
    assert.deepStrictEqual(fields.generated, {
      by: "CodeModeMemoryAgent",
      at: "2026-10-06T12:30:05Z",
    });
    const stamped = renderConcept(
      { type: "Fact", verified: [{ by: "human:user" }, { by: "process:x", at: "then" }] },
      "",
      { by: "Agent", at: NOW },
    );
    assertMatch(parse(stamped.split("---")[1]!, { schema: "yaml-1.1" }), {
      verified: [
        { by: "human:user", at: "2026-10-06T00:00:00Z" },
        { by: "process:x", at: "then" },
      ],
    });
  });

  it("rejects a frontmatter that never opened", () => {
    assert.ok(splitFrontmatter("# hi\n")[2]?.includes("no frontmatter"));
  });
});

describe("an activity filesystem", () => {
  const Store = z.object({ store: z.string() });
  class Memory implements OkfReadable {
    constructor(config: { store: string }) {
      if (config.store === "missing") throw new ConfigError("no such store");
    }
    files: Record<string, Uint8Array> = {
      "notes/a.md": new TextEncoder().encode("---\ntype: Fact\ntitle: A\n---\n"),
      "notes/b.md": new TextEncoder().encode("---\ntype: Fact\n---\nSee [a](a.md).\n"),
      "big.txt": new Uint8Array(256_000 + 10).fill("x".charCodeAt(0)),
    };
    async stat(path: string) {
      if (path === "notes") return { is_dir: true, size: 0, mtime: 1.5 };
      const data = this.files[path];
      return data === undefined ? null : { is_dir: false, size: data.length, mtime: 1.5 };
    }
    async read(path: string, offset = 0, length: number | null = null) {
      const data = this.files[path];
      if (data === undefined)
        throw new FsError("FileNotFoundError", 2, "No such file or directory", path);
      const end = length === null ? undefined : offset + length;
      return data.subarray(offset, end);
    }
    async list(path: string) {
      if (path !== "." && path !== "notes")
        throw new FsError("NotADirectoryError", 20, "Not a directory", path);
      return path === "." ? ["notes"] : ["a.md", "b.md"];
    }
  }
  class Broken extends Memory {
    override async read(): Promise<Uint8Array> {
      throw new FsError("PermissionError", 13, "Permission denied", "secret.md");
    }
  }

  const fs = activityFilesystem({ name: "test-store", configSchema: Store, writable: false });
  const registered = filesystemActivities(fs, Memory);

  it("pages a view and walks a graph from the call's config", async () => {
    const view = registered.activities[activityName("test-store", "view")]!;
    const first = (await view({ config: { store: "s" }, path: "big.txt" })) as {
      offset: number;
      content: string;
      size: number;
    };
    assert.equal(first.offset, 0);
    assert.equal(first.content.length, 256_000);
    assert.equal(first.size, 256_010);
    const graph = (await registered.activities[activityName("test-store", "okf_graph")]!({
      config: { store: "s" },
      path: ".",
      links: true,
    })) as {
      concepts: { id: string; title: string }[];
      links: { source: string; target: string }[];
    };
    assert.deepStrictEqual(
      graph.concepts.map((item) => [item.id, item.title]),
      [
        ["notes/a", "A"],
        ["notes/b", "b"],
      ],
    );
    assert.deepStrictEqual(
      graph.links.map((link) => [link.source, link.target]),
      [["notes/b", "notes/a"]],
    );
    const missing = await registered.activities[activityName("test-store", "stat")]!({
      config: { store: "missing" },
      path: ".",
    }).catch((e: unknown) => e);
    assert.ok(missing instanceof ApplicationFailure);
    assertMatch(missing, { type: "ValueError", nonRetryable: true });
    const directory = await view({ config: { store: "s" }, path: "notes" }).catch(
      (e: unknown) => e,
    );
    assertMatch(directory, { type: "IsADirectoryError", nonRetryable: true });
    const denied = filesystemActivities(fs, Broken);
    const failure = await denied.activities[activityName("test-store", "read")]!({
      config: { store: "s" },
      path: "secret.md",
    }).catch((e: unknown) => e);
    assertMatch(failure, {
      type: "PermissionError",
      message: "Permission denied: 'secret.md'",
    });
  });

  it("refuses a half-writable class and a duplicate registration", () => {
    class Half extends Memory {
      async write() {}
    }
    assert.throws(() => filesystemActivities(fs, Half), /writable filesystem needs all four/);
    assert.throws(
      () => new AgentHarnessPlugin({ filesystems: [registered, registered] }),
      /two filesystems named 'test-store'/,
    );
    assert.throws(
      () => new AgentHarnessPlugin({ filesystems: [{} as never] }),
      /not an ActivityFileSystem/,
    );
  });
});

describe("mounts and the OKF tool", () => {
  const fs = activityFilesystem({
    name: "local-disk",
    configSchema: z.object({ directory: z.string() }),
    writable: true,
  });
  const readOnly = activityFilesystem({
    name: "skills-disk",
    configSchema: z.object({}),
    writable: false,
  });

  it("publishes an empty file index, then records what a session touches", () => {
    const decl = vfsMount("/store", fs, { description: "The test store." });
    const published: unknown[] = [];
    const ref = new StateRef<FileIndex>("files", decl, (event) => published.push(event));
    assertMatch(ref.current, {
      kind: "file_index",
      mount: "/store",
      description: "The test store.",
      read_only: false,
      okf_version: null,
      source: null,
      entries: {},
    });
    const track = indexTrack(ref, { filesystem: "test-store", config: { store: "s" } });
    track.noteList(".", ["logo.bin", "notes"]);
    track.noteMkdir("out");
    track.noteWrite("out/r.md", 3);
    track.noteRemove("logo.bin");
    track.noteStat("notes/a.md", { is_dir: false, size: 5, mtime: 1.5 });
    track.noteRead("notes/a.md", 5);
    const entries = ref.current.entries;
    assert.deepStrictEqual(Object.keys(entries).sort(), ["notes", "notes/a.md", "out", "out/r.md"]);
    assert.equal(entries.notes?.is_dir, true);
    assert.deepStrictEqual(entries["notes/a.md"], {
      is_dir: false,
      size: 5,
      mtime: 1.5,
      writes: 0,
    });
    assert.deepStrictEqual(entries["out/r.md"], { is_dir: false, size: 3, mtime: null, writes: 1 });
  });

  it("explains OKF on a bundle mount and refuses anything else", () => {
    assert.throws(() => vfsMount("/skills", readOnly, { description: "skills" }), /readOnly: true/);
    const bundle = okfBundleVfsMount("/memory", fs, { description: "memory" });
    const ref = new StateRef("memory", bundle, () => {});
    assert.equal(ref.current.okf_version, "0.2");
    const mount = bundle.bind(ref, { directory: "alice" });
    const tool = okfCodeModeTool(mount, { name: "run_code" });
    assert.ok(
      tool.description.includes(
        "Knowledge bundle: `/memory` is an Open Knowledge Format (OKF v0.2)",
      ),
    );
    assert.ok(tool.description.includes("okf_concepts()"));
    assert.throws(
      () => okfCodeModeTool(new Mount("/x", new InMemoryFileSystem()), { name: "run_code" }),
      /okfBundleVfsMount/,
    );
  });
});
