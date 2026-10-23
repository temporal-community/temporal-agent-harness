/**
 * Every frame a run has streamed, kept in IndexedDB so a reload restores the
 * whole run and resumes the stream past it, plus one small view snapshot per run
 * that can be painted before any frame is folded.
 *
 * Runs are keyed `${workflow_id}|${created_at}`: the console has no run id, and
 * a workflow id can be reused by a later run. Nothing the API returns names the
 * backend either, so a run cached from one server is restored against another
 * only when both have a workflow of that id started in the same second; the
 * origin's own storage partition and the week's expiry below bound the rest.
 * One database, opened once at v1;
 * a run's frames are keyed [runKey, seq], so a restore is one ranged getAll.
 *
 * Run and view records carry `version`, and a run's frames are read only through
 * its run record. A record written in a shape this build does not know is
 * ignored and deleted, so changing the shape is a bump here, not a migration.
 *
 * Like agentRunStorage, every failure here means "nothing stored": private
 * mode, a full disk or a blocked upgrade must not fail a session.
 *
 * Runs are evicted least recently used first, by `lastAccess`, to stay under
 * a byte budget and a run count; the run on screen never is. A write the disk
 * refuses evicts and retries once, and failing again leaves the run's stored
 * prefix as it was for the caller to stop extending.
 */
import type { AgentSseFrame, Session } from "$lib/api/types";

/** The shape of every record below. Bump it when that shape changes. */
export const schemaVersion = 1;

/** At most this much, and at most a fifth of what the browser will give the origin. */
const maxStoredBytes = 500e6;
const quotaShare = 0.2;
const maxStoredRuns = 200;
/** A run nobody has opened for this long goes, whatever the budget says. */
const runTtlMs = 7 * 24 * 60 * 60 * 1000;
const writerTouchMs = 60_000;

export type RunRecord = {
  runKey: string;
  workflowId: string;
  lastResumeOffset: number;
  offsets: Record<string, number>;
  count: number;
  lastAccess: number;
  version?: number;
};

export type StoredView<T> = T & {
  runKey: string;
  workflowId: string;
  savedAt: number;
  version?: number;
};

export function runKeyOf(session: Pick<Session, "workflow_id" | "created_at">): string {
  return `${session.workflow_id}|${session.created_at}`;
}

let database: Promise<IDBDatabase | null> | null = null;

function openDatabase(): Promise<IDBDatabase | null> {
  database ??= new Promise((resolve) => {
    if (typeof indexedDB === "undefined") return resolve(null);
    const request = indexedDB.open("temporal-agent-ui.runs", 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      db.createObjectStore("frames", { keyPath: ["runKey", "seq"] });
      db.createObjectStore("runs", { keyPath: "runKey" });
      db.createObjectStore("views", { keyPath: "runKey" });
    };
    request.onsuccess = () => {
      const db = request.result;
      /* Another tab's upgrade waits on this connection, and a closed one fails
         every transaction: either way the next call opens a fresh one. */
      db.onversionchange = () => {
        db.close();
        database = null;
      };
      db.onclose = () => {
        database = null;
      };
      resolve(db);
    };
    request.onerror = request.onblocked = () => {
      database = null;
      resolve(null);
    };
  });
  return database;
}

function settled<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function committed(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = tx.onabort = () => reject(tx.error);
  });
}

function runFrames(runKey: string, count = Infinity): IDBKeyRange {
  return IDBKeyRange.bound([runKey, 0], [runKey, count - 1]);
}

/**
 * One run's record and its frames in stream order, in a single read.
 *
 * Only the record's `count` frames are read, and a run missing any of them is
 * deleted rather than restored: another tab can evict a run while its writer
 * keeps appending, which leaves frames past a gap and a record that counts them.
 */
export async function readRun(
  runKey: string
): Promise<{ record: RunRecord; frames: AgentSseFrame[] } | null> {
  const db = await openDatabase();
  if (!db) return null;
  try {
    const tx = db.transaction(["runs", "frames"], "readonly");
    const [record, rows] = await new Promise<[RunRecord | undefined, { frame: AgentSseFrame }[]]>(
      (resolve, reject) => {
        const get = tx.objectStore("runs").get(runKey);
        get.onerror = () => reject(get.error);
        get.onsuccess = () => {
          const found: RunRecord | undefined = get.result;
          if (!found || found.version !== schemaVersion || found.count <= 0) {
            return resolve([found, []]);
          }
          settled(tx.objectStore("frames").getAll(runFrames(runKey, found.count))).then(
            (frames) => resolve([found, frames]),
            reject
          );
        };
      }
    );
    if (!record) return null;
    if (record.version !== schemaVersion || rows.length !== record.count) {
      void deleteRuns([runKey]);
      return null;
    }
    return { record, frames: rows.map((row) => row.frame) };
  } catch {
    return null;
  }
}

/**
 * Mark a run used now, so one that is open but streams nothing new does not
 * expire. The record is read and written in one transaction, so a touch never
 * puts back an older count than the writer last stored.
 */
export async function touchRun(runKey: string): Promise<void> {
  const db = await openDatabase();
  if (!db) return;
  try {
    const tx = db.transaction("runs", "readwrite");
    const store = tx.objectStore("runs");
    const get = store.get(runKey);
    get.onsuccess = () => {
      const record: RunRecord | undefined = get.result;
      if (record?.version === schemaVersion) store.put({ ...record, lastAccess: Date.now() });
    };
    await committed(tx);
  } catch {
    // Its old lastAccess stands; the run may expire a little early.
  }
}

/** Drop whole runs: each one's frames in one key-range delete, with its records. */
export async function deleteRuns(runKeys: readonly string[]): Promise<void> {
  const db = await openDatabase();
  if (!db || runKeys.length === 0) return;
  try {
    const tx = db.transaction(["frames", "runs", "views"], "readwrite");
    for (const runKey of runKeys) {
      tx.objectStore("frames").delete(runFrames(runKey));
      tx.objectStore("runs").delete(runKey);
      tx.objectStore("views").delete(runKey);
    }
    await committed(tx);
  } catch {
    // Left for the next pass to try again.
  }
}

/**
 * The runs to drop, least recently used first, until `usage` fits `budget` and
 * the count fits `maxRuns`. Records in an unknown shape, and runs unopened for
 * a week, always go; the active run never does.
 *
 * A run's share of `usage` is its share of all stored frames: the browser only
 * reports the origin's total, and sizing every frame on write would cost a
 * serialization per frame.
 */
export function runsToEvict(
  runs: readonly RunRecord[],
  activeRunKey: string | null,
  usage: number,
  budget: number,
  maxRuns = maxStoredRuns,
  now = Date.now()
): string[] {
  const frames = runs.reduce((total, run) => total + run.count, 0) || 1;
  let used = usage;
  let kept = runs.length;
  const evicted: string[] = [];
  const oldestFirst = [...runs].sort((a, b) => a.lastAccess - b.lastAccess);
  for (const run of oldestFirst) {
    if (run.runKey === activeRunKey) continue;
    const current = run.version === schemaVersion && now - run.lastAccess < runTtlMs;
    if (current && used <= budget && kept <= maxRuns) continue;
    evicted.push(run.runKey);
    used -= (usage * run.count) / frames;
    kept -= 1;
  }
  return evicted;
}

/** Bring the store under budget, or under `shrinkTo` of its usage after a refused write. */
export async function evictRuns(activeRunKey: string | null, shrinkTo = 1): Promise<void> {
  const db = await openDatabase();
  if (!db) return;
  try {
    const runs = await settled<RunRecord[]>(
      db.transaction("runs", "readonly").objectStore("runs").getAll()
    );
    const estimate = await globalThis.navigator?.storage?.estimate?.();
    /* Without an estimate, frames stand in for bytes: only `shrinkTo` can then
       evict by size, which is the refused write that needs it. */
    const usage = estimate?.usage || runs.reduce((total, run) => total + run.count, 0);
    const budget = Math.min(maxStoredBytes, (estimate?.quota ?? Infinity) * quotaShare);
    await deleteRuns(runsToEvict(runs, activeRunKey, usage, Math.min(budget, usage * shrinkTo)));
  } catch {
    // Eviction is housekeeping; the next pass tries again.
  }
}

function isQuotaError(error: unknown): boolean {
  return error instanceof Error && error.name === "QuotaExceededError";
}

/**
 * Append frames from `seq` on and update the run's record, in one transaction.
 *
 * Resolves false when the frames are not stored. The transaction is atomic, so
 * the stored prefix is still whole, but a later batch would leave a gap after
 * it: the caller stops writing the run.
 */
export async function appendRunFrames(
  run: RunRecord,
  seq: number,
  frames: readonly AgentSseFrame[]
): Promise<boolean> {
  const db = await openDatabase();
  if (!db) return false;
  const write = (): Promise<void> => {
    const tx = db.transaction(["frames", "runs"], "readwrite");
    const store = tx.objectStore("frames");
    frames.forEach((frame, index) => store.put({ runKey: run.runKey, seq: seq + index, frame }));
    tx.objectStore("runs").put({ ...run, version: schemaVersion });
    return committed(tx);
  };
  try {
    await write();
    return true;
  } catch (error) {
    if (!isQuotaError(error)) return false;
  }
  await evictRuns(run.runKey, 0.5);
  try {
    await write();
    return true;
  } catch {
    return false;
  }
}

/**
 * The run this workflow was last opened as, so a reload can fold its frames
 * before the session list names the run. Records this build does not know are
 * ignored; the newest `lastAccess` wins.
 */
export async function readLatestRun(workflowId: string): Promise<RunRecord | null> {
  const db = await openDatabase();
  if (!db) return null;
  try {
    const runs: RunRecord[] = await settled(
      db
        .transaction("runs", "readonly")
        .objectStore("runs")
        .getAll(IDBKeyRange.bound(`${workflowId}|`, `${workflowId}|\uffff`))
    );
    return runs.reduce<RunRecord | null>((newest, run) => {
      if (run.version !== schemaVersion || run.workflowId !== workflowId || run.count <= 0) {
        return newest;
      }
      return !newest || run.lastAccess > newest.lastAccess ? run : newest;
    }, null);
  } catch {
    return null;
  }
}

/** The newest view snapshot for any run of this workflow id. */
export async function readLatestView<T>(workflowId: string): Promise<StoredView<T> | null> {
  const db = await openDatabase();
  if (!db) return null;
  try {
    const views: StoredView<T>[] = await settled(
      db
        .transaction("views", "readonly")
        .objectStore("views")
        .getAll(IDBKeyRange.bound(`${workflowId}|`, `${workflowId}|\uffff`))
    );
    const unknown = views.filter((view) => view.version !== schemaVersion);
    if (unknown.length > 0) void deleteRuns(unknown.map((view) => view.runKey));
    /* The key range also takes in workflow ids that carry this one and a `|`. */
    return views.reduce<StoredView<T> | null>(
      (newest, view) =>
        view.version !== schemaVersion ||
        view.workflowId !== workflowId ||
        (newest && newest.savedAt >= view.savedAt)
          ? newest
          : view,
      null
    );
  } catch {
    return null;
  }
}

export async function writeView<T>(view: StoredView<T>): Promise<void> {
  const db = await openDatabase();
  if (!db) return;
  try {
    const tx = db.transaction("views", "readwrite");
    tx.objectStore("views").put({ ...view, version: schemaVersion });
    await committed(tx);
  } catch {
    // A stale or missing snapshot only costs the instant paint.
  }
}

/**
 * Become the one tab writing this run, or learn another tab already is.
 *
 * Resolves to the release function, or null when another tab holds the run:
 * two writers would interleave their seqs. Without Web Locks every tab writes,
 * so two tabs on one run can overwrite each other's `[runKey, seq]` frames and a
 * restore can miss events in the middle without noticing. Every supported browser
 * has Web Locks, so that fallback is a known risk, not a supported mode.
 */
export function claimRunWriter(runKey: string): Promise<(() => void) | null> {
  /* The writer marks its run used while it holds it, so another tab's eviction
     sees an open run that streams nothing new as recently used. */
  const held = (release: () => void) => {
    const timer = setInterval(() => void touchRun(runKey), writerTouchMs);
    return () => {
      clearInterval(timer);
      release();
    };
  };
  if (typeof navigator === "undefined" || !navigator.locks) return Promise.resolve(held(() => {}));
  return new Promise((resolve) => {
    void navigator.locks.request(
      `temporal-agent-ui.run:${runKey}`,
      { ifAvailable: true },
      (lock) => {
        if (!lock) return resolve(null);
        return new Promise<void>((release) => resolve(held(() => release())));
      }
    );
  });
}
