import assert from "node:assert/strict";
import { describe, it, vi } from "vitest";

import { installFakeIndexedDb } from "../../../tests/support/fakeIndexedDb.mjs";
import {
  appendRunFrames,
  claimRunWriter,
  deleteRuns,
  readLatestView,
  readRun,
  runsToEvict,
  schemaVersion,
  writeView
} from "./frameStore.ts";

const { stores, options } = installFakeIndexedDb();

const frame = (offset) => ({ event: "reply_delta", data: { resume_offset: offset } });
const run = (runKey, over = {}) => ({
  runKey,
  workflowId: runKey.split("|")[0],
  lastResumeOffset: 1,
  offsets: {},
  count: 1,
  lastAccess: Date.now(),
  ...over
});
const settle = () => new Promise((resolve) => setTimeout(resolve, 10));

/** Overwrite a stored record as an older or newer build would have left it. */
function restamp(store, runKey, version) {
  for (const row of stores.get(store).rows.values()) {
    if (row.value.runKey === runKey) row.value.version = version;
  }
}

describe("frameStore connection", () => {
  it("opens a fresh connection after another tab's upgrade closes this one", async () => {
    await readRun("wf-none|1");
    const open = globalThis.indexedDB.open;
    const db = open().result;
    let opens = 0;
    globalThis.indexedDB.open = (...args) => {
      opens += 1;
      return open(...args);
    };
    try {
      db.onversionchange();
      await readRun("wf-none|1");
      assert.equal(opens, 1, "the closed connection must not be reused");
    } finally {
      globalThis.indexedDB.open = open;
    }
  });
});

describe("frameStore schema version", () => {
  it("ignores and clears a run written in a shape this build does not know", async () => {
    await appendRunFrames(run("wf-old|1"), 0, [frame(1)]);
    await writeView({ runKey: "wf-old|1", workflowId: "wf-old", savedAt: 1 });
    assert.equal((await readRun("wf-old|1"))?.frames.length, 1, "fixture: a current run reads back");

    restamp("runs", "wf-old|1", 99);
    restamp("views", "wf-old|1", 99);
    assert.equal(await readLatestView("wf-old"), null, "an unknown view must not be painted");
    assert.equal(await readRun("wf-old|1"), null, "an unknown run must not be restored");

    await settle();
    for (const name of ["frames", "runs", "views"]) {
      assert.equal(stores.get(name).rows.size, 0, `its ${name} must be deleted`);
    }
  });
});

describe("frameStore eviction", () => {
  const now = Date.now();
  const stored = (runKey, minutesAgo, count = 100) =>
    run(runKey, { lastAccess: now - minutesAgo * 60_000, count, version: schemaVersion });

  it("drops the least recently used runs until usage and count fit, never the active one", () => {
    const runs = [stored("b|1", 3), stored("active|1", 4), stored("c|1", 2), stored("d|1", 1)];
    assert.deepEqual(runsToEvict(runs, "active|1", 400, 400), [], "a store under budget keeps everything");
    assert.deepEqual(
      runsToEvict(runs, "active|1", 400, 250),
      ["b|1", "c|1"],
      "over budget, the oldest go first, and the active run is passed over however old"
    );
    assert.deepEqual(runsToEvict(runs, "active|1", 0, 1e9, 2), ["b|1", "c|1"], "and the same for the count");
    assert.deepEqual(
      runsToEvict([...runs, run("old-shape|1", { lastAccess: now })], "active|1", 0, 1e9),
      ["old-shape|1"],
      "a record in an unknown shape always goes"
    );
  });

  it("drops a run nobody has opened for a week, even under budget", () => {
    const week = 7 * 24 * 60;
    const runs = [stored("stale|1", week + 1), stored("fresh|1", week - 1), stored("active|1", week * 2)];
    assert.deepEqual(runsToEvict(runs, "active|1", 0, 1e9), ["stale|1"]);
  });

  it("evicts and retries once when the disk refuses a write, then reports the run unstored", async () => {
    await appendRunFrames(
      run("wf-stale|1", { lastAccess: 1, count: 10 }),
      0,
      Array.from({ length: 10 }, (_, index) => frame(index + 1))
    );
    await appendRunFrames(run("wf-live|1"), 0, [frame(1)]);
    let refusals = 1;
    options.failPut = (store, value) =>
      store === "frames" && value.runKey === "wf-live|1" && refusals-- > 0
        ? new DOMException("full", "QuotaExceededError")
        : null;

    assert.equal(await appendRunFrames(run("wf-live|1", { count: 2 }), 1, [frame(2)]), true);
    assert.equal(await readRun("wf-stale|1"), null, "an older run is evicted to make room");
    assert.equal((await readRun("wf-live|1"))?.frames.length, 2, "and the retry lands");

    refusals = Infinity;
    assert.equal(
      await appendRunFrames(run("wf-live|1", { count: 3 }), 2, [frame(3)]),
      false,
      "a write refused twice is reported, so the caller stops extending the run"
    );
    const kept = await readRun("wf-live|1");
    assert.deepEqual(
      [kept?.frames.length, kept?.record.count],
      [2, 2],
      "and the stored prefix is left whole"
    );
    options.failPut = () => null;
  });
});

describe("frameStore reads", () => {
  it("drops a run whose head another tab evicted while its writer kept appending", async () => {
    await appendRunFrames(run("wf-gap|1", { count: 2 }), 0, [frame(1), frame(2)]);
    await deleteRuns(["wf-gap|1"]);
    await appendRunFrames(run("wf-gap|1", { count: 3 }), 2, [frame(3)]);

    assert.equal(await readRun("wf-gap|1"), null, "frames past a gap must not be restored as the run");
    await settle();
    assert.equal(
      [...stores.get("frames").rows.values()].filter((row) => row.value.runKey === "wf-gap|1").length,
      0,
      "and the orphaned tail is deleted"
    );
  });

  it("reads only the frames its record counts", async () => {
    await appendRunFrames(run("wf-count|1", { count: 2 }), 0, [frame(1), frame(2)]);
    await appendRunFrames(run("wf-count|1", { count: 2 }), 2, [frame(3)]);
    assert.deepEqual(
      (await readRun("wf-count|1"))?.frames.map((f) => f.data.resume_offset),
      [1, 2]
    );
  });
});

describe("frameStore writer", () => {
  it("marks its run used while it holds it, without putting back an older count", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      await appendRunFrames(run("wf-held|1", { lastAccess: 1, count: 2 }), 0, [frame(1), frame(2)]);
      const stored = () => stores.get("runs").rows.get(JSON.stringify("wf-held|1")).value;
      const release = await claimRunWriter("wf-held|1");

      vi.advanceTimersByTime(60_000);
      await settle();
      assert.ok(stored().lastAccess > 1, "an open run that streams nothing stays recently used");
      assert.equal(stored().count, 2, "and keeps the count its writer stored");

      release();
      stored().lastAccess = 1;
      vi.advanceTimersByTime(60_000);
      await settle();
      assert.equal(stored().lastAccess, 1, "a released run is no longer touched");
    } finally {
      vi.useRealTimers();
    }
  });
});
