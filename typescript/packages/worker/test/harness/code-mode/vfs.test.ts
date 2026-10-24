// The Code Mode virtual filesystem: the in-memory backend, routing and refusals, stepping a script's
// file operations, and scripts on a running agent reading and writing mounts.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import type { ApplicationFailure } from "@temporalio/common";
import { MockActivityEnvironment } from "@temporalio/testing";
import { Worker } from "@temporalio/worker";
import {
  FileTree,
  InMemoryFileSystem,
  Mount,
  StateRef,
  codeModeTool,
  state,
  type AnyHarnessTool,
} from "@temporalio/agent-harness";
import type { CodeBatchStep } from "@temporalio/agent-harness/harness/code-mode";
import {
  performFileOp,
  route,
  validateMounts,
  type FileSystem,
} from "@temporalio/agent-harness/harness/code-mode/vfs";
import {
  TOOL_APPROVAL_UPDATE,
  type AgentEvent,
} from "@temporalio/agent-harness/harness/agent-protocol";
import {
  DEFAULT_CODE_MODE_LIMITS,
  codeResumeBatch,
  codeStartBatch,
} from "../../../src/harness/code-mode/monty-stepper.ts";
import {
  FIXTURES,
  collect,
  replyText,
  send,
  skills,
  startEnv,
  type TestEnv,
} from "../../support.ts";
import { assertMatch } from "../../match.ts";

const text = (data: Uint8Array) => new TextDecoder().decode(data);
const bytes = (s: string) => new TextEncoder().encode(s);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

// Runs each fs_* tool straight against the backend, as the runner would after its approval gate.
const direct = (calls: string[] = []) =>
  (async (t: AnyHarnessTool, input: Record<string, unknown>, backend: FileSystem) => {
    calls.push(t.name);
    const path = String(input.path);
    switch (t.name) {
      case "fs_seed":
        return (backend as InMemoryFileSystem).seedNow();
      case "fs_stat":
        return backend.stat(path);
      case "fs_read":
        return {
          __monty__: "bytes",
          base64: Buffer.from(await backend.read(path)).toString("base64"),
        };
      case "fs_list":
        return backend.list(path);
      case "fs_write":
        return backend.write!(
          path,
          input.encoding === "base64"
            ? Buffer.from(String(input.content), "base64")
            : bytes(String(input.content)),
        );
      case "fs_mkdir":
        return backend.mkdir!(path);
      case "fs_delete":
        return backend.delete!(path);
      case "fs_rename":
        return backend.rename!(path, String(input.target));
      default:
        throw new Error(t.name);
    }
  }) satisfies Parameters<typeof performFileOp>[2];

const op = (
  operation: string,
  path: string,
  args: unknown[] = [],
  kwargs: Record<string, unknown> = {},
) => ({ call_id: 1, operation, path, args, kwargs });

describe("InMemoryFileSystem", () => {
  it("writes, reads, lists, renames directories whole, and deletes", async () => {
    const fs = new InMemoryFileSystem();
    await fs.mkdir("notes");
    await fs.write("notes/a.md", bytes("hello"));
    await fs.mkdir("notes/sub");
    await fs.write("notes/sub/b.md", bytes("x"));
    assert.deepEqual(await fs.list("notes"), ["a.md", "sub"]);
    assert.deepEqual(await fs.stat("notes/a.md"), { is_dir: false, size: 5 });
    await fs.rename("notes", "moved");
    assert.equal(text(await fs.read("moved/sub/b.md")), "x");
    assert.equal(await fs.stat("notes"), null);
    await assert.rejects(fs.delete("moved"), {
      type: "OSError",
      message: "[Errno 39] Directory not empty: 'moved'",
    });
    await assert.rejects(fs.read("nope"), { type: "FileNotFoundError" });
    await assert.rejects(fs.write("missing/x", bytes("x")), { type: "FileNotFoundError" });
  });

  it("enforces maxBytes before the write, counting a replaced file once", async () => {
    const fs = new InMemoryFileSystem({ maxBytes: 10 });
    await fs.write("a", bytes("12345678"));
    await fs.write("a", bytes("1234567890"));
    await assert.rejects(fs.write("b", bytes("1")), {
      type: "OSError",
      message: /limited to 10 bytes/,
    });
    assert.equal(await fs.stat("b"), null);
  });

  it("seeds once, refusing a path outside the mount", async () => {
    let calls = 0;
    const fs = new InMemoryFileSystem({
      seed: async () => (calls++, { "s/one.md": "1", "two.bin": new Uint8Array([0xff, 0]) }),
    });
    assert.equal(fs.needsSeed, true);
    assert.deepEqual(await fs.seedNow(), [2, 3]);
    assert.deepEqual([fs.needsSeed, calls, await fs.list(".")], [false, 1, ["s", "two.bin"]]);
    await assert.rejects(
      new InMemoryFileSystem({ seed: async () => ({ "../x": "no" }) }).seedNow(),
      /inside the mount/,
    );
  });

  it("a seed that fails leaves the filesystem unseeded, to seed again next time", async () => {
    const badPath = new InMemoryFileSystem({ seed: async () => ({ "../x": "no" }) });
    await assert.rejects(badPath.seedNow(), /inside the mount/);
    assert.equal(badPath.needsSeed, true);
    let size = 20;
    const tooBig = new InMemoryFileSystem({
      maxBytes: 10,
      seed: async () => ({ a: "x".repeat(size) }),
    });
    await assert.rejects(tooBig.seedNow(), { type: "OSError" });
    assert.equal(tooBig.needsSeed, true);
    size = 5;
    assert.deepEqual(await tooBig.seedNow(), [1, 5]);
    assert.equal(tooBig.needsSeed, false);
  });

  it("with state, keeps its files in a FileTree and publishes one patch per change", async () => {
    const events: { type: string; ops?: Record<string, unknown>[] }[] = [];
    const ref = new StateRef("workspace", state(FileTree), (e) => events.push(e));
    const fs = new InMemoryFileSystem({ state: ref as unknown as StateRef<FileTree> });
    await fs.mkdir("d");
    await fs.write("d/a.txt", bytes("hi"));
    await fs.write("bin", new Uint8Array([0xff]));
    assert.deepEqual(ref.current, {
      files: {
        "d/a.txt": { content: "hi", encoding: "utf-8", size: 2 },
        bin: { content: "/w==", encoding: "base64", size: 1 },
      },
      directories: ["d"],
    });
    // A file write publishes its file and nothing else.
    assert.deepEqual(
      events.map((e) =>
        e.type === "state_patch" ? e.ops!.map((o) => `${o.op} ${o.path}`) : e.type,
      ),
      [["add /directories/-"], ["add /files/d~1a.txt"], ["add /files/bin"]],
    );
  });
});

describe("mounts and routing", () => {
  const ws = new Mount("/workspace", new InMemoryFileSystem());
  const ro = new Mount(
    "/skills",
    new InMemoryFileSystem({ seed: async () => ({ "a/SKILL.md": "do it" }) }),
    { readOnly: true },
  );

  it("validates mount paths and backends, and refuses overlapping mounts", () => {
    assert.throws(() => new Mount("workspace", new InMemoryFileSystem()), /absolute, normalized/);
    assert.throws(() => new Mount("/a/../b", new InMemoryFileSystem()), /absolute, normalized/);
    assert.throws(() => new Mount("/", new InMemoryFileSystem()), /root/);
    assert.throws(
      () =>
        new Mount("/r", {
          stat: async () => null,
          read: async () => new Uint8Array(),
          list: async () => [],
        }),
      /writable/,
    );
    assert.throws(
      () => validateMounts([ws, new Mount("/workspace/sub", new InMemoryFileSystem())]),
      /overlap/,
    );
  });

  it("routes by whole path segments", () => {
    assertMatch(route([ws], "/workspace/a/b"), { relative: "a/b" });
    assertMatch(route([ws], "/workspace"), { relative: "." });
    assert.equal(route([ws], "/workspaces/a"), null);
  });

  it("refuses read-only writes, cross-mount renames and paths outside every mount without a backend call", async () => {
    const calls: string[] = [];
    await assert.rejects(
      performFileOp([ws, ro], op("Path.write_text", "/skills/a/x.md", ["x"]), direct(calls)),
      { type: "PermissionError", message: "[Errno 13] Permission denied: '/skills/a/x.md'" },
    );
    await assert.rejects(
      performFileOp([ws, ro], op("open", "/skills/a/SKILL.md", ["a"]), direct(calls)),
      { type: "PermissionError" },
    );
    await assert.rejects(
      performFileOp([ws, ro], op("Path.rename", "/workspace/a", ["/skills/a"]), direct(calls)),
      { type: "OSError", message: /Invalid cross-device link/ },
    );
    assert.deepEqual(
      await performFileOp([ws, ro], op("Path.read_text", "/etc/passwd"), direct(calls)),
      { __monty__: "not_handled" },
    );
    assert.deepEqual(
      await performFileOp(
        [ws, ro],
        op("Path.read_text", "/workspace/../etc/passwd"),
        direct(calls),
      ),
      { __monty__: "not_handled" },
    );
    assert.deepEqual(calls, []);
  });

  it("reads open()'s mode by position or as mode=, and opens 'x' only when the file doesn't exist", async () => {
    await assert.rejects(
      performFileOp([ws, ro], op("open", "/skills/a/SKILL.md", [], { mode: "w" }), direct()),
      { type: "PermissionError" },
    );
    const fs = new InMemoryFileSystem();
    const mount = new Mount("/w", fs);
    assert.deepEqual(
      await performFileOp([mount], op("open", "/w/new.txt", [], { mode: "w" }), direct()),
      { __monty__: "handle", path: "/w/new.txt", mode: "w" },
    );
    assert.deepEqual(await fs.stat("new.txt"), { is_dir: false, size: 0 });
    assert.deepEqual(await performFileOp([mount], op("open", "/w/x.txt", ["x"]), direct()), {
      __monty__: "handle",
      path: "/w/x.txt",
      mode: "x",
    });
    assert.deepEqual(await fs.stat("x.txt"), { is_dir: false, size: 0 });
    await assert.rejects(
      performFileOp([mount], op("open", "/w/x.txt", [], { mode: "x" }), direct()),
      { type: "FileExistsError", message: "[Errno 17] File exists: '/w/x.txt'" },
    );
  });

  it("seeds before the first operation, and names the virtual path in a backend's error", async () => {
    const calls: string[] = [];
    assert.equal(
      await performFileOp([ws, ro], op("Path.read_text", "/skills/a/SKILL.md"), direct(calls)),
      "do it",
    );
    assert.deepEqual(calls, ["fs_seed", "fs_stat", "fs_read"]);
    await assert.rejects(
      performFileOp([ws, ro], op("Path.read_text", "/workspace/missing.md"), direct()),
      {
        type: "FileNotFoundError",
        message: "[Errno 2] No such file or directory: '/workspace/missing.md'",
      },
    );
    await assert.rejects(
      performFileOp([ws], op("Path.write_text", "/workspace/no/dir.md", ["x"]), direct()),
      { message: "[Errno 2] No such file or directory: '/workspace/no/dir.md'" },
    );
  });
});

describe("stepping a script's file operations", () => {
  const SUM_INTO_REPORT = `
from pathlib import Path
rows = Path('/skills/report/data.csv').read_text().splitlines()
total = sum(int(r.split(',')[1]) for r in rows)
Path('/workspace/out').mkdir()
with open('/workspace/out/report.md', 'w') as f:
    f.write(f'total: {total}')
[Path('/skills/report/SKILL.md').read_text(), Path('/workspace/out/report.md').read_text()]
`;

  it("each file operation ends a step, and the next resumes with its result", async () => {
    const mounts = [
      new Mount("/skills", new InMemoryFileSystem({ seed: async () => ({ ...skills.files }) }), {
        readOnly: true,
      }),
      new Mount("/workspace", new InMemoryFileSystem()),
    ];
    const codeTool = codeModeTool([], { name: "run_code", mounts });
    const env = new MockActivityEnvironment();
    const clock = { now: 0, seed: 1 };
    const secret = "test-only-continuation-secret-0123456789";
    const operations: string[] = [];
    let step: CodeBatchStep = await env.run(
      codeStartBatch,
      SUM_INTO_REPORT,
      codeTool.codeModeStubs,
      DEFAULT_CODE_MODE_LIMITS,
      clock,
      secret,
      true,
    );
    while (!step.done) {
      const fileOp = step.file_op!;
      operations.push(fileOp.operation);
      assert.deepEqual(step.awaiting, [fileOp.call_id]);
      const result = await performFileOp(mounts, fileOp, direct()).then(
        (return_value) => ({ call_id: fileOp.call_id, return_value }),
        (e: ApplicationFailure) => ({
          call_id: fileOp.call_id,
          return_value: null,
          error: e.message,
          error_type: e.type ?? "RuntimeError",
        }),
      );
      step = await env.run(
        codeResumeBatch,
        {
          snapshot: step.snapshot!,
          results: [result],
          type_names: codeTool.codeModeStubs.type_names,
          clock,
          files: true,
        },
        DEFAULT_CODE_MODE_LIMITS,
        secret,
      );
    }
    assert.equal(step.error, null);
    assert.deepEqual(JSON.parse(step.output_json!), ["Sum the amounts in data.csv.", "total: 5"]);
    assert.ok(operations.includes("open"));
  });
});

describe("Code Mode scripts on mounts", () => {
  let env: TestEnv;
  before(async () => {
    env = await startEnv("code-mode-vfs");
  });
  after(async () => {
    await env.stop();
  });

  const py = (body: string) =>
    `import asyncio\nfrom pathlib import Path\nasync def main():\n${body.replace(/^/gm, "    ")}\nasyncio.run(main())`;
  const approveAll =
    (h: { workflowId: string }, approved: string[] = []) =>
    async (e: AgentEvent) => {
      if (e.event.type === "tool_approval_requested") {
        approved.push(e.event.tool_name);
        // A later collect replays earlier turns' requests, which are already answered.
        await env.client.workflow
          .getHandle(h.workflowId)
          .executeUpdate(TOOL_APPROVAL_UPDATE, {
            args: [{ tool_id: e.event.tool_id, approved: true }],
          })
          .catch(() => undefined);
      }
    };

  it("reads a seeded skill, writes the workspace through approved fs_* calls, and publishes the files as state", async () => {
    const h = await env.start("VfsProbeAgent");
    await send(h, "run", {
      text: py(
        'skill = Path("/skills/report/SKILL.md").read_text()\nrows = Path("/skills/report/data.csv").read_text().splitlines()\ntotal = sum(int(r.split(",")[1]) for r in rows)\nPath("/workspace/out").mkdir()\nwith open("/workspace/out/report.md", "w") as f:\n    f.write(f"total: {total}\\n")\nreturn [skill, total, sorted(p.name for p in Path("/workspace/out").iterdir())]',
      ),
    });
    const approved: string[] = [];
    const events = await collect(env.client, h.workflowId, { onEvent: approveAll(h, approved) });
    assert.equal(replyText(events), "result: ['Sum the amounts in data.csv.', 5, ['report.md']]");
    assert.deepEqual(approved, ["fs_mkdir", "fs_write", "fs_write"]);
    const fsTools = events.flatMap((e) =>
      e.event.type === "tool_start" && e.event.tool_name.startsWith("fs_")
        ? [{ name: e.event.tool_name, id: e.event.tool_id }]
        : [],
    );
    assert.equal(fsTools[0]!.name, "fs_seed");
    for (const { id } of fsTools) assert.match(id, UUID);
    const patches = events.filter((e) => e.event.type === "state_patch");
    assert.ok(patches.length >= 3);
  });

  it("keeps the workspace across scripts, and a read-only write raises PermissionError in the script", async () => {
    const h = await env.start("VfsProbeAgent");
    await send(h, "run", {
      text: py('Path("/workspace/note.txt").write_text("kept")\nreturn "ok"'),
    });
    await collect(env.client, h.workflowId, { onEvent: approveAll(h) });
    await send(h, "run", {
      text: py(
        'try:\n    Path("/skills/x.md").write_text("no")\nexcept PermissionError as e:\n    denied = str(e)\nreturn [Path("/workspace/note.txt").read_text(), denied]',
      ),
    });
    const events = await collect(env.client, h.workflowId, {
      until: (e) => e.event.type === "turn_end" && e.turn_number === 2,
      onEvent: approveAll(h),
    });
    assert.equal(
      replyText(events.filter((e) => e.turn_number === 2)),
      `result: ['kept', "[Errno 13] Permission denied: '/skills/x.md'"]`,
    );
  });

  it("a write past maxBytes raises OSError in the script and changes nothing", async () => {
    const h = await env.start("VfsProbeAgent");
    await send(h, "run", {
      text: py(
        'try:\n    Path("/workspace/big").write_text("x" * 500)\nexcept OSError as e:\n    return ["refused", Path("/workspace/big").exists()]',
      ),
    });
    assert.equal(
      replyText(await collect(env.client, h.workflowId, { onEvent: approveAll(h) })),
      "result: ['refused', False]",
    );
  });

  it("a host call made before a file operation starts at once, and one never awaited is cancelled when the script ends", async () => {
    const h = await env.start("VfsProbeAgent");
    await send(h, "run", {
      text: py('pending = book(id="X1")\nreturn Path("/skills/report/SKILL.md").read_text()'),
    });
    const events = await collect(env.client, h.workflowId);
    assert.equal(replyText(events), "result: 'Sum the amounts in data.csv.'");
    assert.ok(
      events.some(
        (e) => e.event.type === "tool_approval_requested" && e.event.tool_name === "book",
      ),
    );
    assert.ok(!events.some((e) => e.event.type === "tool_end" && e.event.tool_name === "book"));
    // Never approved, it was cancelled before it ran (its approval stays listed: a known issue in both harnesses).
    const scheduled = ((await h.fetchHistory()).events ?? []).flatMap((e) =>
      e.activityTaskScheduledEventAttributes
        ? [e.activityTaskScheduledEventAttributes.activityType?.name]
        : [],
    );
    assert.ok(!scheduled.includes("book"));
  });

  it("replays, even after the seeded files changed", async () => {
    const h = await env.start("VfsProbeAgent");
    await send(h, "run", {
      text: py(
        'Path("/workspace/a.txt").write_text(Path("/skills/report/data.csv").read_text())\nreturn Path("/workspace/a.txt").read_text()',
      ),
    });
    assert.equal(
      replyText(await collect(env.client, h.workflowId, { onEvent: approveAll(h) })),
      "result: 'a,2\\nb,3\\n'",
    );
    const before = skills.files;
    skills.files = { "report/data.csv": "changed" };
    try {
      const history = await h.fetchHistory();
      for await (const result of Worker.runReplayHistories({ workflowsPath: FIXTURES }, [
        { workflowId: h.workflowId, history },
      ]))
        assert.equal(result.error, undefined);
    } finally {
      skills.files = before;
    }
  });
});
