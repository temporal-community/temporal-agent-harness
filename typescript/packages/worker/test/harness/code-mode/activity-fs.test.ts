// Activity filesystems over a real directory: their operations, the Python exception a failed one
// is (a Node fs error included), the OKF walk past what it can't read, and a script on a running
// agent catching a disk error by name.

import assert from "node:assert/strict";
import {
  lstat,
  mkdir,
  mkdtemp,
  readdir,
  readFile,
  rename,
  rm,
  rmdir,
  symlink,
  unlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { after, before, describe, it } from "node:test";
import { ApplicationFailure } from "@temporalio/common";
import { Client, Connection } from "@temporalio/client";
import { NativeConnection, Worker } from "@temporalio/worker";
import { activityName, type FileStat } from "@temporalio/agent-harness";
import { AgentHarnessPlugin } from "../../../src/plugin.ts";
import {
  filesystemActivities,
  type FilesystemOps,
} from "../../../src/harness/code-mode/activity-fs.ts";
import { codeModeActivities } from "../../../src/harness/code-mode/monty-stepper.ts";
import { buildOkfGraph, type OkfReadable } from "../../../src/harness/code-mode/okf.ts";
import { assertMatch } from "../../match.ts";
import {
  TEST_CONTINUATION_SECRET,
  collect,
  replyText,
  send,
  startServer,
  type TemporalServer,
} from "../../support.ts";
import { diskFilesystem } from "./disk-workflows.ts";

const WORKFLOWS = fileURLToPath(new URL("./disk-workflows.js", import.meta.url));

/** What Node throws for a failed system call, for errors a test can't provoke as root. */
const nodeError = (code: string, errno: number, description: string, path: string) =>
  Object.assign(new Error(`${code}: ${description}, open '${path}'`), {
    code,
    errno: -errno,
    syscall: "open",
    path,
  });

let root = "";

/** A filesystem over `root`, letting every Node fs error through as a real one would. */
class Disk implements FilesystemOps {
  constructor(_config: Record<string, never>) {}
  #at(path: string) {
    return join(root, path);
  }
  async stat(path: string): Promise<FileStat | null> {
    try {
      const found = await lstat(this.#at(path));
      return { is_dir: found.isDirectory(), size: found.size, mtime: found.mtimeMs / 1000 };
    } catch (e) {
      if ((e as { code?: string }).code === "ENOENT") return null;
      throw e;
    }
  }
  async read(path: string, offset = 0, length: number | null = null): Promise<Uint8Array> {
    const data = await readFile(this.#at(path));
    return data.subarray(offset, length === null ? undefined : offset + length);
  }
  async list(path: string) {
    return readdir(this.#at(path));
  }
  async write(path: string, data: Uint8Array) {
    await writeFile(this.#at(path), data);
  }
  async mkdir(path: string) {
    await mkdir(this.#at(path));
  }
  async delete(path: string) {
    const at = this.#at(path);
    await ((await lstat(at)).isDirectory() ? rmdir(at) : unlink(at));
  }
  async rename(path: string, target: string) {
    await rename(this.#at(path), this.#at(target));
  }
}

const registered = filesystemActivities(diskFilesystem, Disk);
const call = (operation: string, input: Record<string, unknown>) =>
  registered.activities[activityName("test-disk", operation)]!({ config: {}, ...input });
const failureOf = (operation: string, input: Record<string, unknown>) =>
  call(operation, input).then(
    () => assert.fail(`${operation} succeeded`),
    (e: unknown) => e,
  );

before(async () => {
  root = await mkdtemp(join(tmpdir(), "activity-fs-"));
});
after(async () => {
  await rm(root, { recursive: true, force: true });
});

describe("an activity filesystem over a directory", () => {
  it("writes text and base64 bytes, makes a directory, renames and deletes", async () => {
    await call("mkdir", { path: "notes" });
    await call("write", { path: "notes/a.txt", content: "hello" });
    await call("write", {
      path: "notes/b.bin",
      content: Buffer.from([0, 255, 1]).toString("base64"),
      encoding: "base64",
    });
    assert.deepEqual([...(await readFile(join(root, "notes/b.bin")))], [0, 255, 1]);
    assert.deepEqual(await call("read", { path: "notes/b.bin" }), {
      content: "AP8B",
      encoding: "base64",
    });
    await call("rename", { path: "notes/a.txt", target: "notes/c.txt" });
    await call("delete", { path: "notes/b.bin" });
    assert.deepEqual(await call("list", { path: "notes" }), ["c.txt"]);
    assert.deepEqual(await call("read", { path: "notes/c.txt" }), {
      content: "hello",
      encoding: "utf-8",
    });
    await call("delete", { path: "notes/c.txt" });
    await call("delete", { path: "notes" });
    assert.deepEqual(await readdir(root), []);
  });

  it("fails a Node fs error non-retryably as the Python built-in for its errno, with Python's message", async () => {
    await mkdir(join(root, "d"));
    await writeFile(join(root, "f.txt"), "x");
    for (const [operation, input, type, message] of [
      ["read", { path: "missing.txt" }, "FileNotFoundError", "No such file or directory: 'missing.txt'"],
      ["mkdir", { path: "d" }, "FileExistsError", "File exists: 'd'"],
      ["read", { path: "d" }, "IsADirectoryError", "Is a directory: 'd'"],
      ["list", { path: "f.txt" }, "NotADirectoryError", "Not a directory: 'f.txt'"],
      ["delete", { path: "nope" }, "FileNotFoundError", "No such file or directory: 'nope'"],
    ] as const) {
      const failure = await failureOf(operation, input);
      assert.ok(failure instanceof ApplicationFailure, `${operation} ${input.path}`);
      assertMatch(failure, { type, message, nonRetryable: true });
    }
    await rm(join(root, "d"), { recursive: true });
    await rm(join(root, "f.txt"));
  });

  it("fails a permission or other OS error as PermissionError or OSError, and a refused argument as ValueError", async () => {
    class Refusing extends Disk {
      override async read(path: string): Promise<Uint8Array> {
        if (path === "locked") throw nodeError("EACCES", 13, "permission denied", path);
        if (path === "full") throw nodeError("ENOSPC", 28, "no space left on device", path);
        return super.read(path);
      }
    }
    const refusing = filesystemActivities(diskFilesystem, Refusing);
    const read = (path: string) =>
      refusing.activities[activityName("test-disk", "read")]!({ config: {}, path }).catch(
        (e: unknown) => e,
      );
    assertMatch(await read("locked"), {
      type: "PermissionError",
      message: "Permission denied: 'locked'",
      nonRetryable: true,
    });
    assertMatch(await read("full"), {
      type: "OSError",
      message: "No space left on device: 'full'",
      nonRetryable: true,
    });
    const bad = await read("bad\0name");
    assert.ok(bad instanceof ApplicationFailure);
    assertMatch(bad, { type: "ValueError", nonRetryable: true });
    assert.ok(bad.message.startsWith("test-disk: "));
  });

  it("lets any other error through to be retried", async () => {
    class Flaky extends Disk {
      override async read(): Promise<Uint8Array> {
        throw new Error("connection reset");
      }
    }
    const failure = await filesystemActivities(diskFilesystem, Flaky)
      .activities[activityName("test-disk", "read")]!({ config: {}, path: "x" })
      .catch((e: unknown) => e);
    assert.ok(!(failure instanceof ApplicationFailure));
    assert.equal((failure as Error).message, "connection reset");
  });
});

describe("an OKF walk over Node fs errors", () => {
  it("skips a directory it can't list and an entry it can't stat, and lists a concept it can't read", async () => {
    const concept = new TextEncoder().encode("---\ntype: Fact\n---\n");
    const fs: OkfReadable = {
      async list(path) {
        if (path === "locked") throw nodeError("EACCES", 13, "permission denied", path);
        return path === "." ? ["a.md", "gone.md", "locked", "secret.md"] : [];
      },
      async stat(path) {
        if (path === "gone.md") throw nodeError("ENOENT", 2, "no such file or directory", path);
        return { is_dir: path === "locked", size: concept.length };
      },
      async read(path) {
        if (path === "secret.md") throw nodeError("EACCES", 13, "permission denied", path);
        return concept;
      },
    };
    const graph = await buildOkfGraph(fs, { prefix: ".", links: true, now: new Date(0) });
    assert.deepEqual(
      graph.concepts.map((c) => [c.id, c.type, c.problem]),
      [
        ["a", "Fact", null],
        ["secret", "Unknown", "unreadable: Permission denied"],
      ],
    );
  });

  it("still fails on an error that is not an OS error", async () => {
    const fs: OkfReadable = {
      list: async () => ["a.md"],
      stat: async () => ({ is_dir: false, size: 1 }),
      read: async () => {
        throw new TypeError("a bug");
      },
    };
    await assert.rejects(buildOkfGraph(fs, { prefix: ".", links: true, now: new Date(0) }), {
      message: "a bug",
    });
  });
});

describe("a script on an activity-backed mount", () => {
  let server: TemporalServer;
  let nativeConnection: NativeConnection;
  let connection: Connection;
  let client: Client;
  let worker: Worker;
  let running: Promise<void>;
  const taskQueue = `activity-fs-${randomUUID()}`;
  before(async () => {
    server = await startServer();
    nativeConnection = await NativeConnection.connect({ address: server.address });
    worker = await Worker.create({
      connection: nativeConnection,
      namespace: server.namespace,
      taskQueue,
      workflowsPath: WORKFLOWS,
      activities: codeModeActivities({ continuationSecret: TEST_CONTINUATION_SECRET }),
      plugins: [new AgentHarnessPlugin({ filesystems: [registered] })],
    });
    running = worker.run();
    connection = await Connection.connect({ address: server.address });
    client = new Client({ connection, namespace: server.namespace });
  });
  after(async () => {
    worker.shutdown();
    await running;
    await nativeConnection.close();
    await connection.close();
    await server.teardown();
  });

  it("writes, renames and deletes files, and catches a disk error by its Python name, failed once and not retried", async () => {
    await symlink(join(root, "nowhere"), join(root, "dangling.txt"));
    const workflowId = `disk-${randomUUID()}`;
    const handle = await client.workflow.start("DiskProbeAgent", {
      taskQueue,
      workflowId,
      args: [{}],
    });
    try {
      await send(handle, "run", {
        text: [
          "from pathlib import Path",
          "Path('/disk/notes').mkdir()",
          "Path('/disk/notes/a.txt').write_text('hello')",
          "Path('/disk/notes/b.bin').write_bytes(b'\\x00\\xff\\x01')",
          "raw = list(Path('/disk/notes/b.bin').read_bytes())",
          "Path('/disk/notes/a.txt').rename('/disk/notes/c.txt')",
          "Path('/disk/notes/b.bin').unlink()",
          "try:",
          "    Path('/disk/dangling.txt').read_text()",
          "    missing = 'read'",
          "except FileNotFoundError as e:",
          "    missing = str(e)",
          "try:",
          "    Path('/disk/notes/c.txt/x').exists()",
          "    below = 'found'",
          "except NotADirectoryError as e:",
          "    below = str(e)",
          "[sorted(str(p) for p in Path('/disk/notes').iterdir()), Path('/disk/notes/c.txt').read_text(), raw, missing, below]",
        ].join("\n"),
      });
      assert.equal(
        replyText(await collect(client, workflowId)),
        "result: [['/disk/notes/c.txt'], 'hello', [0, 255, 1], \"No such file or directory: 'dangling.txt'\", \"Not a directory: 'notes/c.txt/x'\"]",
      );
      const history = (await handle.fetchHistory()).events ?? [];
      const failed = history.flatMap((e) =>
        e.activityTaskFailedEventAttributes ? [e.activityTaskFailedEventAttributes] : [],
      );
      assert.deepEqual(
        failed.map((f) => [
          f.failure?.applicationFailureInfo?.type,
          f.failure?.applicationFailureInfo?.nonRetryable,
          history.find((e) => String(e.eventId) === String(f.startedEventId))
            ?.activityTaskStartedEventAttributes?.attempt,
        ]),
        [
          ["FileNotFoundError", true, 1],
          ["NotADirectoryError", true, 1],
        ],
      );
    } finally {
      await handle.terminate("test finished").catch(() => undefined);
    }
  });
});
