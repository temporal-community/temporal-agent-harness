/**
 * Does the language badge stay pinned when a code block scrolls sideways?
 *
 * Same CDP-over-chrome-headless-shell approach as ui/tools/shots/shoot.mjs.
 * Measures the badge's right edge against the pre's right edge at scrollLeft 0
 * and at the far right, and shoots both.
 *
 * Usage: node ui/tools/codebadge-shots/shoot.mjs <label>
 */
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";

const BROWSER =
  "/Users/cshep/Library/Caches/ms-playwright/chromium_headless_shell-1234" +
  "/chrome-headless-shell-mac-arm64/chrome-headless-shell";
const URL_ = "http://127.0.0.1:5173/tools/codebadge-shots/index.html";
const label = process.argv[2] ?? "badge";
const outDir = "/tmp/shots";

const browser = spawn(
  BROWSER,
  [
    "--remote-debugging-port=9337",
    "--user-data-dir=/tmp/badge-shot-profile",
    "--force-color-profile=srgb",
    "--window-size=1000,900",
    "about:blank"
  ],
  { stdio: "ignore" }
);

let endpoint = null;
for (let i = 0; i < 40 && !endpoint; i += 1) {
  await new Promise((resolve) => setTimeout(resolve, 250));
  endpoint = await fetch("http://127.0.0.1:9337/json/version")
    .then((r) => r.json())
    .then((v) => v.webSocketDebuggerUrl)
    .catch(() => null);
}
if (!endpoint) throw new Error("browser never opened a debugging port");

const socket = new WebSocket(endpoint);
await new Promise((resolve) => socket.addEventListener("open", resolve, { once: true }));

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
const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
const page = (method, params) => send(method, params, sessionId);

await page("Page.enable");
await page("Runtime.enable");
await page("Emulation.setDeviceMetricsOverride", {
  width: 1000,
  height: 900,
  deviceScaleFactor: 2,
  mobile: false
});

const evaluate = async (expression) => {
  const { result, exceptionDetails } = await page("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true
  });
  if (exceptionDetails) throw new Error(exceptionDetails.text ?? "evaluate failed");
  return result.value;
};

await page("Page.navigate", { url: URL_ });
await new Promise((resolve) => setTimeout(resolve, 3500));
if (!(await evaluate("document.querySelectorAll('[data-shot]').length"))) {
  throw new Error("harness did not render — is the dev server up?");
}

/* The badge is a ::before with no node of its own, so nothing can read its box.
   What decides whether it scrolls is which ancestor is its containing block: an
   absolutely positioned pseudo-element is laid out against the nearest
   positioned ancestor, and if that ancestor is the scroll container then the
   badge lives in scrolled coordinates. Assert the pre is not it, and that
   whatever is does not scroll. */
const measure = (shot) =>
  evaluate(`(() => {
    const pre = document.querySelector('[data-shot="${shot}"] pre.md-code-block');
    const host = pre.offsetParent;
    const hostStyle = getComputedStyle(host);
    return {
      overflow: pre.scrollWidth - pre.clientWidth,
      scrollLeft: pre.scrollLeft,
      prePosition: getComputedStyle(pre).position,
      badgeHost: host.className,
      badgeHostScrolls: hostStyle.overflowX !== "visible" || hostStyle.overflowY !== "visible"
    };
  })()`);

async function shoot(name, shot, pad = 8) {
  const box = await evaluate(`(() => {
    const el = document.querySelector('[data-shot="${shot}"] pre.md-code-block');
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  })()`);
  const { data } = await page("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
    clip: {
      x: Math.max(0, box.x - pad),
      y: Math.max(0, box.y - pad),
      width: box.width + pad * 2,
      height: box.height + pad * 2,
      scale: 2
    }
  });
  const file = `${outDir}/${label}-${name}.png`;
  await writeFile(file, Buffer.from(data, "base64"));
  console.log(`${file}  ${Math.round(box.width)}x${Math.round(box.height)}`);
}

await mkdir(outDir, { recursive: true });

console.log("wrapped (shipped wrap fix):", JSON.stringify(await measure("wrapped")));
await shoot("wrapped", "wrapped");

console.log("scrolls, at rest:", JSON.stringify(await measure("scrolls")));
await shoot("scrolls-at-rest", "scrolls");

await evaluate(`(() => {
  const pre = document.querySelector('[data-shot="scrolls"] pre.md-code-block');
  pre.scrollLeft = pre.scrollWidth;
})()`);
await new Promise((resolve) => setTimeout(resolve, 200));
console.log("scrolls, scrolled right:", JSON.stringify(await measure("scrolls")));
await shoot("scrolls-scrolled", "scrolls");

await send("Target.closeTarget", { targetId });
browser.kill();
socket.close();
