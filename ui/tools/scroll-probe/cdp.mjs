/**
 * Minimal CDP plumbing shared by the scroll probes.
 *
 * Tracked, unlike the probes around it, because verify-drawer-settle.mjs imports it
 * and a tracked file's import has to survive a clone. It still needs a browser binary
 * at the path below and a running stack, so it can never be part of the check suite.
 */
import { spawn } from "node:child_process";

export const BROWSER =
  "/Users/cshep/Library/Caches/ms-playwright/chromium_headless_shell-1234" +
  "/chrome-headless-shell-mac-arm64/chrome-headless-shell";

/* Scrollbars are hidden by default because every probe here measures geometry, and a
   bar that steals 15px of clientWidth changes the numbers. The overflow probe wants
   them: a scrollbar the reader complains about has to be visible to be screenshotted. */
export async function open({ port, profile, extraArgs = [], hideScrollbars = true }) {
  const browser = spawn(
    BROWSER,
    [
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      ...(hideScrollbars ? ["--hide-scrollbars"] : []),
      "--force-color-profile=srgb",
      "--window-size=1600,1000",
      ...extraArgs,
      "about:blank"
    ],
    { stdio: "ignore" }
  );

  let endpoint = null;
  for (let i = 0; i < 60 && !endpoint; i += 1) {
    await new Promise((r) => setTimeout(r, 250));
    endpoint = await fetch(`http://127.0.0.1:${port}/json/version`)
      .then((r) => r.json())
      .then((v) => v.webSocketDebuggerUrl)
      .catch(() => null);
  }
  if (!endpoint) throw new Error("browser never opened a debugging port");

  const socket = new WebSocket(endpoint);
  await new Promise((r) => socket.addEventListener("open", r, { once: true }));

  let nextId = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const settle = pending.get(message.id);
    if (!settle) return;
    pending.delete(message.id);
    if (message.error) settle.reject(new Error(JSON.stringify(message.error)));
    else settle.resolve(message.result);
  });

  function send(method, params = {}, sessionId) {
    const id = (nextId += 1);
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params, sessionId }));
    });
  }

  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", {
    targetId,
    flatten: true
  });

  const api = {
    send: (method, params) => send(method, params, sessionId),
    async eval(expression, { awaitPromise = true } = {}) {
      const res = await send(
        "Runtime.evaluate",
        { expression, returnByValue: true, awaitPromise },
        sessionId
      );
      if (res.exceptionDetails) {
        throw new Error(
          `page threw: ${JSON.stringify(res.exceptionDetails.exception?.description ?? res.exceptionDetails)}`
        );
      }
      return res.result.value;
    },
    close() {
      socket.close();
      browser.kill("SIGTERM");
    }
  };
  await api.send("Page.enable");
  await api.send("Runtime.enable");
  return api;
}
