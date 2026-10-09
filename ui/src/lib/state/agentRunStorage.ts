/**
 * What a run remembers across a reload: which session was on screen
 * (localStorage + URL `s=`), per-session desks, operator prefs, and the frames
 * already seen for the active session (sessionStorage, one tab's worth).
 *
 * Every function here guards `typeof window`, for the server-render and
 * self-check paths where there is no storage at all, and swallows its own
 * failures: private mode, a filled quota and a blocked cookie jar all throw,
 * and none of that is worth failing a session over. Each fallback means
 * "nothing stored", which every caller already has to handle.
 */
import type { AgentSseFrame } from "$lib/api/types";

const activeSessionStorageKey = "temporal-agent-ui.active-session.v1";
const frameCacheStorageKeyPrefix = "temporal-agent-ui.frames.v1:";
const desksStorageKey = "temporal-agent-ui.desks.v1";
const prefsStorageKey = "temporal-agent-ui.prefs.v1";
/** Cap how many session desks we keep so quota failures stay rare. */
const MAX_STORED_DESKS = 40;

export type StoredPane = {
  id: string;
  kind: string;
  collapsed: boolean;
  pinned: boolean;
  size: number | null;
  touchedAt: number;
  joinedAt: number;
  split: boolean;
  share: number | null;
};

export type StoredDesk = StoredPane[][];

export type OperatorPrefs = {
  transcriptFilter?: string;
  drawerHeight?: number;
  sessionDrawerWidth?: number;
  followDefault?: boolean;
  /** Fold settled tool cards on the graph. Absent means yes; see agentRun. */
  graphFocus?: boolean;
};

function frameCacheStorageKey(sessionId: string): string {
  return `${frameCacheStorageKeyPrefix}${sessionId}`;
}

export function readUrlSessionId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const value = new URLSearchParams(window.location.search).get("s");
    return value && value.trim() ? value.trim() : null;
  } catch {
    return null;
  }
}

export function writeUrlSessionId(sessionId: string): void {
  if (typeof window === "undefined") return;
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.get("s") === sessionId) return;
    url.searchParams.set("s", sessionId);
    const next = `${url.pathname}${url.search}`;
    if (next !== `${window.location.pathname}${window.location.search}`) {
      window.history.replaceState(null, "", next);
    }
  } catch {
    // Ignore URL write failures.
  }
}

export function readStoredActiveSessionId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const value = window.localStorage.getItem(activeSessionStorageKey);
    return value && value.trim() ? value : null;
  } catch {
    return null;
  }
}

export function writeStoredActiveSessionId(sessionId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(activeSessionStorageKey, sessionId);
  } catch {
    // Ignore storage failures; active session persistence is a UI convenience.
  }
}

export function readCachedFrames(sessionId: string): AgentSseFrame[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.sessionStorage.getItem(frameCacheStorageKey(sessionId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as { frames?: unknown };
    return Array.isArray(parsed.frames) ? (parsed.frames as AgentSseFrame[]) : [];
  } catch {
    return [];
  }
}

/**
 * Most of the frame JSON one session may cache, in UTF-16 units. Browsers give
 * sessionStorage about 5M of them per origin, shared by every key.
 */
export const maxCachedFrameChars = 2_000_000;

/**
 * Cache a prefix of a session's frames, given as their comma-joined JSON.
 *
 * Only ever a prefix: each workflow's stream resumes past the highest offset
 * already seen, so frames cut off the end are refetched rather than lost. When
 * the quota still refuses it, other sessions' frames are dropped first.
 */
export function writeCachedFrameJson(sessionId: string, framesJson: string): void {
  if (typeof window === "undefined") return;
  const key = frameCacheStorageKey(sessionId);
  const value = `{"frames":[${framesJson}],"savedAt":${Date.now()}}`;
  try {
    window.sessionStorage.setItem(key, value);
    return;
  } catch {
    // Quota, most likely; make room below.
  }
  try {
    const store = window.sessionStorage;
    for (let i = store.length - 1; i >= 0; i -= 1) {
      const other = store.key(i);
      if (other?.startsWith(frameCacheStorageKeyPrefix) && other !== key) store.removeItem(other);
    }
    window.sessionStorage.setItem(key, value);
  } catch {
    try {
      window.sessionStorage.removeItem(key);
    } catch {
      // Ignore storage failures.
    }
  }
}

function readDesksMap(): Record<string, { desk: StoredDesk; savedAt: number }> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(desksStorageKey);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, { desk?: StoredDesk; savedAt?: number }>;
    if (!parsed || typeof parsed !== "object") return {};
    const out: Record<string, { desk: StoredDesk; savedAt: number }> = {};
    for (const [sessionId, value] of Object.entries(parsed)) {
      if (!value || !Array.isArray(value.desk)) continue;
      out[sessionId] = { desk: value.desk, savedAt: Number(value.savedAt) || 0 };
    }
    return out;
  } catch {
    return {};
  }
}

export function readStoredDesk(sessionId: string): StoredDesk | null {
  const entry = readDesksMap()[sessionId];
  return entry?.desk ?? null;
}

export function writeStoredDesk(sessionId: string, desk: StoredDesk): void {
  if (typeof window === "undefined") return;
  try {
    const map = readDesksMap();
    map[sessionId] = { desk, savedAt: Date.now() };
    const pruned = Object.entries(map)
      .sort((a, b) => b[1].savedAt - a[1].savedAt)
      .slice(0, MAX_STORED_DESKS);
    window.localStorage.setItem(
      desksStorageKey,
      JSON.stringify(Object.fromEntries(pruned))
    );
  } catch {
    // Ignore storage failures; desk memory is a convenience.
  }
}

export function readOperatorPrefs(): OperatorPrefs {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(prefsStorageKey);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as OperatorPrefs;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function writeOperatorPrefs(prefs: OperatorPrefs): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      prefsStorageKey,
      JSON.stringify({ ...readOperatorPrefs(), ...prefs })
    );
  } catch {
    // Ignore storage failures.
  }
}
