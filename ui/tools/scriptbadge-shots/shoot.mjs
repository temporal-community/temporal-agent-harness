/**
 * Does the activity pane's language badge stay pinned when the script detail scrolls down?
 *
 * Copy of ui/tools/codebadge-shots/shoot.mjs pointed at the real AgentChatPanel: same
 * chrome-headless-shell-over-CDP approach, same reasoning about what to measure. Needs
 * the vite dev server up on 5173.
 *
 * Usage: node ui/tools/scriptbadge-shots/shoot.mjs <label>
 */
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";

const BROWSER =
  "/Users/cshep/Library/Caches/ms-playwright/chromium_headless_shell-1234" +
  "/chrome-headless-shell-mac-arm64/chrome-headless-shell";
const URL_ = "http://127.0.0.1:5173/tools/scriptbadge-shots/index.html";
const label = process.argv[2] ?? "scriptbadge";
const outDir = "/tmp/shots";

const browser = spawn(
  BROWSER,
  [
    "--remote-debugging-port=9339",
    "--user-data-dir=/tmp/scriptbadge-shot-profile",
    "--force-color-profile=srgb",
    "--window-size=1120,1000",
    "about:blank"
  ],
  { stdio: "ignore" }
);

let endpoint = null;
for (let i = 0; i < 40 && !endpoint; i += 1) {
  await new Promise((resolve) => setTimeout(resolve, 250));
  endpoint = await fetch("http://127.0.0.1:9339/json/version")
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
  width: 1120,
  height: 1000,
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
await new Promise((resolve) => setTimeout(resolve, 4000));
if (!(await evaluate("document.querySelectorAll('[data-shot]').length"))) {
  throw new Error("harness did not render — is the dev server up?");
}

/* The activity list is behind the turn summary, and the script detail behind the row. */
const opened = await evaluate(`(() => {
  document.querySelectorAll('.activity-summary').forEach((el) => el.click());
  return document.querySelectorAll('.activity-summary').length;
})()`);
await new Promise((resolve) => setTimeout(resolve, 400));
const rows = await evaluate(`(() => {
  const hit = [...document.querySelectorAll('.activity-row-button')];
  hit.forEach((el) => el.click());
  return hit.length;
})()`);
await new Promise((resolve) => setTimeout(resolve, 400));
console.log(`opened ${opened} turn summaries, ${rows} activity rows`);

/* The badge is a ::before with no node of its own, so nothing can read its box. What
   decides whether it scrolls is which ancestor is its containing block: an absolutely
   positioned pseudo-element is laid out against the nearest positioned ancestor, and
   if that ancestor is the scroll container then the badge lives in scrolled
   coordinates. Assert the pre is not it, and that whatever is does not scroll. */
const measure = (shot) =>
  evaluate(`(() => {
    const pre = document.querySelector('[data-shot="${shot}"] pre.activity-script-detail');
    if (!pre) return null;
    /* The containing block is the pre itself when the pre is positioned; only when it
       is static does the badge fall through to the nearest positioned ancestor. */
    const prePosition = getComputedStyle(pre).position;
    const host = prePosition === "static" ? pre.offsetParent : pre;
    const hostStyle = getComputedStyle(host);
    return {
      overflow: pre.scrollHeight - pre.clientHeight,
      scrollTop: pre.scrollTop,
      prePosition,
      badgeHost: host.className || host.tagName,
      badgeHostScrolls: hostStyle.overflowX !== "visible" || hostStyle.overflowY !== "visible"
    };
  })()`);

async function shoot(name, shot, pad = 8) {
  const box = await evaluate(`(() => {
    const el = document.querySelector('[data-shot="${shot}"] pre.activity-script-detail');
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

const scrollDown = (shot) =>
  evaluate(`(() => {
    const pre = document.querySelector('[data-shot="${shot}"] pre.activity-script-detail');
    pre.scrollTop = pre.scrollHeight;
  })()`);

await mkdir(outDir, { recursive: true });

for (const shot of ["unfixed", "fixed"]) {
  console.log(`${shot}, at rest:`, JSON.stringify(await measure(shot)));
  await shoot(`${shot}-at-rest`, shot);
  await scrollDown(shot);
  await new Promise((resolve) => setTimeout(resolve, 250));
  console.log(`${shot}, scrolled down:`, JSON.stringify(await measure(shot)));
  await shoot(`${shot}-scrolled`, shot);
}

await send("Target.closeTarget", { targetId });
browser.kill();
socket.close();
