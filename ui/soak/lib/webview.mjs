/**
 * Bun.WebView({ backend: "chrome" }) + evaluate helpers for DOM soaks.
 * Loud fail if Chrome / Playwright chrome-headless-shell is missing.
 */
import { existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const CHROME_HINT =
  "Install Chrome/Chromium/Edge/Brave, or Playwright's chromium " +
  "(npx playwright install chromium), or set BUN_CHROME_PATH.";

/** Prefer an explicit binary Bun can spawn (Playwright cache included). */
export function resolveChromePath() {
  if (process.env.BUN_CHROME_PATH && existsSync(process.env.BUN_CHROME_PATH)) {
    return process.env.BUN_CHROME_PATH;
  }
  const home = homedir();
  const roots = [
    join(home, "Library/Caches/ms-playwright"),
    join(home, ".cache/ms-playwright"),
    join(process.env.LOCALAPPDATA ?? "", "ms-playwright")
  ].filter(Boolean);

  const candidates = [];
  for (const root of roots) {
    if (!existsSync(root)) continue;
    for (const entry of readdirSync(root)) {
      if (entry.startsWith("chromium-") && !entry.includes("headless")) {
        candidates.push(
          join(
            root,
            entry,
            "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
          ),
          join(
            root,
            entry,
            "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
          ),
          join(root, entry, "chrome-linux/chrome"),
          join(root, entry, "chrome-win/chrome.exe")
        );
      }
      if (entry.startsWith("chromium_headless_shell-")) {
        candidates.push(
          join(
            root,
            entry,
            "chrome-headless-shell-mac-arm64/chrome-headless-shell"
          ),
          join(
            root,
            entry,
            "chrome-headless-shell-mac-x64/chrome-headless-shell"
          ),
          join(root, entry, "chrome-headless-shell-linux64/chrome-headless-shell"),
          join(root, entry, "chrome-headless-shell-win64/chrome-headless-shell.exe")
        );
      }
    }
  }
  for (const path of candidates) {
    if (existsSync(path)) return path;
  }
  return null;
}

function chromeError(cause) {
  const message = cause instanceof Error ? cause.message : String(cause);
  return new Error(
    `Bun.WebView chrome backend failed (${message}). ${CHROME_HINT}`
  );
}

/**
 * @param {{ headless?: boolean, width?: number, height?: number }} [opts]
 */
export function openChromeView(opts = {}) {
  const path = resolveChromePath();
  try {
    return new Bun.WebView({
      backend: path ? { type: "chrome", path } : "chrome",
      headless: opts.headless !== false,
      width: opts.width ?? 1280,
      height: opts.height ?? 800
    });
  } catch (error) {
    throw chromeError(error);
  }
}

/** Navigate and evaluate; rewrite opaque pipe deaths into the install hint. */
export async function withChromeView(fn, opts = {}) {
  let view;
  try {
    view = openChromeView(opts);
  } catch (error) {
    throw error;
  }
  try {
    return await fn(view);
  } catch (error) {
    throw chromeError(error);
  } finally {
    try {
      view?.close();
    } catch {
      // ignore close races after a dead pipe
    }
  }
}

/**
 * Serve a static HTML string on an ephemeral port; return base URL + close.
 * @param {string} html
 */
export async function serveHtml(html) {
  const server = Bun.serve({
    port: 0,
    fetch() {
      return new Response(html, {
        headers: { "content-type": "text/html; charset=utf-8" }
      });
    }
  });
  return {
    url: `http://127.0.0.1:${server.port}/`,
    close: () => server.stop(true)
  };
}
