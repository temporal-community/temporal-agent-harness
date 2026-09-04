/**
 * The three layers that adopted `dismissable`, driven rather than reasoned about.
 *
 * check-dismissable.mjs covers the layer logic against a stubbed DOM, which is where the
 * stacking and the focus rule are pinned. What it cannot see is the wiring: whether the
 * selector each site passes as `keep` actually matches the surface it meant, and whether a
 * press on an anchor now closes its layer in one press instead of closing and reopening it.
 * That needs a real browser, which is why this lives here and not in ui/scripts/.
 *
 * Also guards the thing most at risk from a window-level Escape handler: with no layer
 * open the attachment has no listeners at all, so the desk's own Escape — the way out of
 * a bled pane, spelled in replayHotkeys' binding table — has to be untouched.
 *
 *   node ui/tools/verify-dismissable.mjs [port]
 */
import { open } from "./scroll-probe/cdp.mjs";

const PORT = process.argv[2] ?? "5173";
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const page = await open({ port: 9440, profile: "/tmp/dismissable-profile" });

await page.send("Emulation.setDeviceMetricsOverride", {
  width: 1680,
  height: 954,
  deviceScaleFactor: 1,
  mobile: false
});

const mouse = async (x, y) => {
  for (const type of ["mousePressed", "mouseReleased"]) {
    await page.send("Input.dispatchMouseEvent", {
      type,
      x,
      y,
      button: "left",
      buttons: type === "mousePressed" ? 1 : 0,
      clickCount: 1
    });
  }
  await wait(450);
};
const key = async (k, code = k) => {
  for (const type of ["keyDown", "keyUp"]) {
    await page.send("Input.dispatchKeyEvent", { type, key: k, code, windowsVirtualKeyCode: 27 });
  }
  await wait(450);
};
/* A press at the centre of whatever the selector names, which is how a reader presses it. */
const pressOn = async (selector) => {
  const at = await page.eval(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    const b = el.getBoundingClientRect();
    if (b.width === 0 || b.height === 0) return null;
    return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) };
  })()`);
  if (!at) return false;
  await mouse(at.x, at.y);
  return true;
};
const shown = (selector) =>
  page.eval(`!!document.querySelector(${JSON.stringify(selector)})`);
/* Which named surface the keyboard ended up on, so "went back to the anchor", "followed the
   press" and "was dropped on the body" are three different answers rather than one. */
const focusWhere = () =>
  page.eval(`(() => {
    const a = document.activeElement;
    if (!a || a === document.body) return "body";
    if (a.closest(".session-anchor")) return "session-anchor";
    if (a.closest(".session-popover")) return "inside-session-popover";
    if (a.closest(".launcher")) return "launcher";
    if (a.closest(".step-controller")) return "step-controller";
    return a.tagName.toLowerCase() + (a.className ? "." + String(a.className).split(" ")[0] : "");
  })()`);

await page.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/?p=chat,graph&p2=latency` });
for (let i = 0; i < 30; i += 1) {
  await wait(700);
  if (await page.eval("!!document.querySelector('.app')").catch(() => false)) break;
}
await wait(3000);
/* A session with a trace behind it, so the transport has a readout to open a card on. */
await pressOn(".session-anchor");
await wait(1200);
await page.eval(`document.querySelectorAll("button.session-row")[5]?.click()`);
await wait(18000);

let bad = 0;
const report = {};
const claim = (ok, what) => {
  console.log(`${ok ? "OK" : "XX"} ${what}`);
  if (!ok) bad += 1;
};

// --- the session menu: keep, Escape, and where focus lands -------------------
await pressOn(".session-anchor");
await wait(900);
claim(await shown(".session-popover"), "the session anchor opens its menu");
await pressOn(".session-popover .session-popover-head");
claim(await shown(".session-popover"), "a press inside the menu leaves it open");
await pressOn(".session-anchor");
report.afterAnchorPress = await shown(".session-popover");
claim(
  report.afterAnchorPress === false,
  "a press on the anchor shuts it in one press, rather than dismissing and reopening"
);

await pressOn(".session-anchor");
await wait(900);
await key("Escape");
report.escapeFocus = await focusWhere();
claim(
  (await shown(".session-popover")) === false,
  "Escape shuts the menu"
);
claim(
  report.escapeFocus === "session-anchor",
  `Escape gives the keyboard back to the anchor it came in on (landed on ${report.escapeFocus})`
);

/* The stranding rescue the Escape claim above covers is the half the old `restoreFocus`
   flag got wrong; these two are the half it got right, which the shared rule must not
   undo — a press that lands somewhere is a move to there, and the caret goes with it.
   Twice, on the two kinds of somewhere: a control in the chrome, and one in the footer. */
await pressOn(".session-anchor");
await wait(900);
await pressOn(".minimap .ticks");
report.elsewhereFocus = await focusWhere();
claim((await shown(".session-popover")) === false, "a press outside shuts the menu");
claim(
  report.elsewhereFocus !== "session-anchor" && report.elsewhereFocus !== "body",
  `the keyboard follows a press onto another control rather than bouncing to the anchor ` +
    `(landed on ${report.elsewhereFocus})`
);

await pressOn(".session-anchor");
await wait(900);
await pressOn(".step-controller .now .readout");
report.deliberateFocus = await focusWhere();
claim(
  report.deliberateFocus !== "session-anchor",
  `a press on another control is a move to it, not a bounce back to the anchor (landed on ` +
    `${report.deliberateFocus})`
);

// --- the transport's now-card: the footer stays usable under it --------------
if ((await shown("#now-card")) === false) await pressOn(".step-controller .now .readout");
await wait(700);
claim(await shown("#now-card"), "the readout opens the now-card");
await pressOn(".step-controller .scrub-lane, .step-controller input.scrub-input");
claim(
  await shown("#now-card"),
  "scrubbing the transport under the card leaves it open — the footer is `keep`"
);
await pressOn(".minimap .ticks");
claim((await shown("#now-card")) === false, "a press outside the footer shuts the card");
await pressOn(".step-controller .now .readout");
await wait(700);
await key("Escape");
claim((await shown("#now-card")) === false, "Escape shuts the card");

// --- the pane launcher ------------------------------------------------------
claim(await pressOn(".launcher button"), "the launcher has a button to press");
await wait(700);
report.launcherOpened = await shown(".launch-menu");
claim(report.launcherOpened, "the launcher opens its menu");
await pressOn(".launcher button");
claim(
  (await shown(".launch-menu")) === false,
  "a press on the launcher's own button shuts it in one press"
);
await pressOn(".launcher button");
await wait(700);
await key("Escape");
claim((await shown(".launch-menu")) === false, "Escape shuts the launcher");

// --- and the desk's own Escape, which no layer is claiming -------------------
/* With nothing open the attachment has removed both listeners, so this is the untouched
   path: F bleeds the focused pane and Escape is what brings the desk back. */
const bleedState = async () =>
  page.eval(`document.querySelector(".app")?.classList.contains("bleed") ?? null`);
claim((await shown(".session-popover")) === false, "sanity: no layer is open");
await pressOn(".pane-shell .pane-content, .pane-shell");
await key("f", "KeyF");
report.bled = await bleedState();
claim(report.bled === true, "F still bleeds a pane with no layer open");
await key("Escape");
report.unbled = await bleedState();
claim(report.unbled === false, "Escape still brings the desk back — the binding table is intact");

console.log(JSON.stringify(report, null, 2));
page.close();
process.exitCode = bad === 0 ? 0 : 1;
