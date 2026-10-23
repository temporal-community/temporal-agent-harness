/**
 * What a run remembers across a reload: which session was on screen
 * (localStorage + URL `s=`), per-session desks and operator prefs. The frames
 * already seen are in IndexedDB (frameStore.ts).
 *
 * Every function here guards `typeof window`, for the server-render and
 * self-check paths where there is no storage at all, and swallows its own
 * failures: private mode, a filled quota and a blocked cookie jar all throw,
 * and none of that is worth failing a session over. Each fallback means
 * "nothing stored", which every caller already has to handle.
 */
const activeSessionStorageKey = "temporal-agent-ui.active-session.v1";
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
  /** Session Manager pins, most recently pinned first. Read through `readPinnedSessions`. */
  pinnedSessions?: string[];
  /** Hidden from the Session Manager's groups, most recently archived first. Nothing is deleted. */
  archivedSessions?: string[];
  /** The Archived group is open. Absent means closed. */
  sessionArchiveOpen?: boolean;
};

/** Enough for any list a person curates by hand; past it the oldest pin drops. */
export const MAX_PINNED_SESSIONS = 100;
/**
 * Archiving clears out a restart's worth of gone sessions at a time, so it holds more: past the
 * cap the oldest archive silently returns to the list. About 250 KB of ids at most, and pruning
 * on write (`toggleArchivedSession`) keeps it to sessions that still exist.
 */
export const MAX_ARCHIVED_SESSIONS = 5_000;

/* Stored ids come from any version of the app, or a hand-edited store: keep only ids. */
function validSessionIds(value: unknown, cap: number): string[] {
  if (!Array.isArray(value)) return [];
  const ids = value.filter((id): id is string => typeof id === "string" && id.trim() !== "");
  return [...new Set(ids)].slice(0, cap);
}

export function validPinnedSessions(value: unknown): string[] {
  return validSessionIds(value, MAX_PINNED_SESSIONS);
}

export function validArchivedSessions(value: unknown): string[] {
  return validSessionIds(value, MAX_ARCHIVED_SESSIONS);
}

export function readArchivedSessions(): string[] {
  return validArchivedSessions(readOperatorPrefs().archivedSessions);
}

/**
 * Archive `workflowId` first, or unarchive it. Like a pin, an unlisted id is kept, unless
 * `listed` is every session there is: then an id missing from it has no session left to hide.
 * Never pass a partial list, or archives behind a filter or a short page come back.
 */
export function toggleArchivedSession(workflowId: string, listed?: readonly string[]): string[] {
  const archived = readArchivedSessions();
  const toggled = archived.includes(workflowId) ? archived.filter((id) => id !== workflowId) : [workflowId, ...archived];
  const known = listed && new Set(listed);
  const next = validArchivedSessions(known ? toggled.filter((id) => known.has(id)) : toggled);
  writeOperatorPrefs({ archivedSessions: next });
  return next;
}

export function readPinnedSessions(): string[] {
  return validPinnedSessions(readOperatorPrefs().pinnedSessions);
}

/**
 * Pin `workflowId` first, or unpin it, and return the pins now stored. A pin whose session is
 * not listed stays: a filter or a shorter page may only be hiding it.
 */
export function togglePinnedSession(workflowId: string): string[] {
  const pins = readPinnedSessions();
  const next = validPinnedSessions(
    pins.includes(workflowId) ? pins.filter((id) => id !== workflowId) : [workflowId, ...pins]
  );
  writeOperatorPrefs({ pinnedSessions: next });
  return next;
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
