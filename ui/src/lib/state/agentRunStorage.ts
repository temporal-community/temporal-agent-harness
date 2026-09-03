/**
 * What a run remembers across a reload: which session was on screen
 * (localStorage, so it outlives the tab) and the frames already seen for it
 * (sessionStorage, one tab's worth).
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

function frameCacheStorageKey(sessionId: string): string {
  return `${frameCacheStorageKeyPrefix}${sessionId}`;
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

export function writeCachedFrames(sessionId: string, frames: AgentSseFrame[]): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(
      frameCacheStorageKey(sessionId),
      JSON.stringify({ frames, savedAt: Date.now() })
    );
  } catch {
    try {
      window.sessionStorage.removeItem(frameCacheStorageKey(sessionId));
    } catch {
      // Ignore storage failures.
    }
  }
}
