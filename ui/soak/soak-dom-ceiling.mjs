/**
 * DOM ceiling soak — chat / transcript / session picker node counts.
 *
 * Minimal fixture HTML (no Temporal stack). Bun.WebView chrome backend for CDP.
 * No virtualization today — this locks a regression that would explode the DOM.
 *
 * Budgets (calibrated 2026-09-04): fixture injects N rows with a small constant
 * of child nodes each; total elements stay under messages×4 + logs×2 + sessions×2
 * + shell (~80). Hard-fail on count mismatch (duplication) or total ceiling.
 *
 * Run: bun ui/soak/soak-dom-ceiling.mjs
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { resolveChromePath, serveHtml, withChromeView } from "./lib/webview.mjs";

const MESSAGES = 500;
const LOGS = 2_000;
const SESSIONS = 200;
/** Fixture shell + ~2–3 nodes per row; leave headroom for markup churn. */
const TOTAL_ELEMENT_CEILING =
  MESSAGES * 4 + LOGS * 2 + SESSIONS * 2 + 80;

const htmlPath = join(
  dirname(fileURLToPath(import.meta.url)),
  "fixtures/dom-ceiling.html"
);
const html = readFileSync(htmlPath, "utf8");

const chromePath = resolveChromePath();
const { url, close: closeServer } = await serveHtml(html);

try {
  const metrics = await withChromeView(async (view) => {
    await view.navigate(
      `${url}?messages=${MESSAGES}&logs=${LOGS}&sessions=${SESSIONS}`
    );
    return view.evaluate(`(() => {
      const messages = document.querySelectorAll(".message").length;
      const logs = document.querySelectorAll(".log-line").length;
      const sessions = document.querySelectorAll(".session-row").length;
      const elements = document.querySelectorAll("*").length;
      return { messages, logs, sessions, elements, injected: window.__soakCounts };
    })()`);
  });

  if (metrics.messages !== MESSAGES) {
    throw new Error(
      `.message count ${metrics.messages} !== injected ${MESSAGES} (duplication or drop)`
    );
  }
  if (metrics.logs !== LOGS) {
    throw new Error(`.log-line count ${metrics.logs} !== injected ${LOGS}`);
  }
  if (metrics.sessions !== SESSIONS) {
    throw new Error(
      `.session-row count ${metrics.sessions} !== injected ${SESSIONS}`
    );
  }
  if (metrics.elements > TOTAL_ELEMENT_CEILING) {
    throw new Error(
      `total elements ${metrics.elements} exceeds ceiling ${TOTAL_ELEMENT_CEILING}`
    );
  }

  console.log(
    `soak-dom-ceiling OK (messages ${metrics.messages}, logs ${metrics.logs}, ` +
      `sessions ${metrics.sessions}, elements ${metrics.elements}/${TOTAL_ELEMENT_CEILING}` +
      `${chromePath ? `; chrome ${chromePath}` : ""})`
  );
} finally {
  closeServer();
}
