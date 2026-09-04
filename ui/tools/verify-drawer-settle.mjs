/**
 * Verification-only: the four things that landed together, and the one that is a
 * policy rather than a layout — the drawer settling instead of creeping.
 *
 * The creep is only observable over time, so this watches the height across a live
 * load rather than sampling it once: a fit that has settled reports the same number
 * on every later sample, and a fit still subscribed to content reports a bigger one.
 *
 *   node ui/tools/verify-drawer-settle.mjs [port]
 */
import { mkdir, writeFile } from "node:fs/promises";
import { open } from "./scroll-probe/cdp.mjs";

const PORT = process.argv[2] ?? "8000";
const OUT = "/tmp/drawer-settle";
await mkdir(OUT, { recursive: true });

const VIEWPORT = { width: 1680, height: 954 };
const CAP_60 = Math.round(VIEWPORT.height * 0.6);
const CAP_AUTO = Math.round(VIEWPORT.height * 0.5);
/* The floor the drawer snaps to when nothing was measured. Ending up at it is the
   cold-load failure this rule exists to avoid, so it is what "locked tiny" means. */
const DRAWER_MIN_GUESS = 96;

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const page = await open({ port: 9430, profile: "/tmp/drawer-settle-profile" });
const shot = async (name) => {
  const { data } = await page.send("Page.captureScreenshot", { format: "png" });
  await writeFile(`${OUT}/${name}.png`, Buffer.from(data, "base64"));
  return `${OUT}/${name}.png`;
};
const mouse = (type, x, y) =>
  page.send("Input.dispatchMouseEvent", {
    type,
    x,
    y,
    button: "left",
    buttons: type === "mouseReleased" ? 0 : 1,
    clickCount: 1
  });

await page.send("Emulation.setDeviceMetricsOverride", {
  ...VIEWPORT,
  deviceScaleFactor: 1,
  mobile: false
});

const state = `(() => {
  const drawer = document.querySelector(".drawer");
  const turns = document.querySelector(".drawer .turns");
  const rows = turns ? [...turns.children] : [];
  const last = rows.at(-1);
  const db = drawer?.getBoundingClientRect();
  return {
    drawerPx: db ? Math.round(db.height) : 0,
    cap60: Math.round(window.innerHeight * 0.6),
    capAuto: Math.round(window.innerHeight * 0.5),
    rows: rows.length,
    collapsed: rows.filter((r) => r.classList.contains("collapsed")).length,
    expanded: rows.filter((r) => !r.classList.contains("collapsed")).length,
    /* Deleted outright, so absence is the assertion. */
    scaleNote: !!document.querySelector(".scale-note, .axis-row"),
    /* Every row keeps its own ruler — the only thing left saying the scales differ. */
    rulers: document.querySelectorAll(".drawer .turn-row:not(.collapsed) .axis .tick").length,
    gapBelowLastPx: last && db ? Math.round(db.bottom - last.getBoundingClientRect().bottom) : null,
    /* Content taller than its box means the trace is scrolling, which is what a capped
       drawer is expected to do rather than grow or cut the last row off. */
    scrolls: turns ? turns.scrollHeight > turns.clientHeight + 1 : null,
    /* The transport aside, in the order the pane header uses. */
    asideOrder: (() => {
      const t = document.querySelector(".step-controller");
      if (!t) return null;
      const chip = t.querySelector(".now");
      const button = t.querySelector('.aside button[aria-label$="the bottom drawer"]');
      if (!chip || !button) return null;
      const cb = chip.getBoundingClientRect(), bb = button.getBoundingClientRect();
      return {
        chipFirst: cb.right <= bb.left,
        gapPx: Math.round(bb.left - cb.right),
        buttonToEdgePx: Math.round(t.getBoundingClientRect().right - bb.right),
        pressed: button.getAttribute("aria-pressed")
      };
    })(),
    nowCardOpen: !!document.querySelector("#now-card")
  };
})()`;

const boot = async (url) => {
  await page.send("Page.navigate", { url });
  for (let i = 0; i < 30; i += 1) {
    await wait(700);
    if (await page.eval("!!document.querySelector('.app')").catch(() => false)) break;
  }
  await wait(2500);
};
const pickSession = async (row) => {
  await page.eval(`document.querySelector(".session-anchor")?.click()`);
  await wait(1400);
  await page.eval(`document.querySelectorAll("button.session-row")[${row}]?.click()`);
};

const report = { shots: {}, viewport: VIEWPORT, cap60: CAP_60, capAuto: CAP_AUTO };

// --- cold load: restored from a link, then the trace streams in --------------
await boot(`http://127.0.0.1:${PORT}/?p=graph&p2=latency`);
await pickSession(5);
/* Sampled across the load and well past it: the last samples must agree, and none
   of them may exceed the cap an unattended fit is allowed. */
const samples = [];
for (let i = 0; i < 14; i += 1) {
  await wait(2000);
  samples.push((await page.eval(state)).drawerPx);
}
await page.eval(`document.querySelector(".session-anchor[aria-expanded='true']")?.click()`);
await wait(1500);
report.coldLoad = { samples, ...(await page.eval(state)) };
report.shots.coldLoad = await shot("1-cold-load");

// --- collapse: the trivial turns fold, the rich ones do not ------------------
report.turnShapes = await page.eval(`(() => {
  return [...document.querySelectorAll(".drawer .turn-row")].map((row) => ({
    label: row.querySelector(".turn-no")?.textContent.trim() ?? null,
    dur: row.querySelector(".turn-dur")?.textContent.trim() ?? null,
    collapsed: row.classList.contains("collapsed"),
    px: Math.round(row.getBoundingClientRect().height),
    foldable: !!row.querySelector(".turn-toggle")
  }));
})()`);

// --- the playhead's turn stays open, and scroll-follow still lands on it -----
report.playhead = await page.eval(`(() => {
  const head = document.querySelector(".drawer .turn-row .playhead");
  const row = head?.closest(".turn-row");
  const turns = document.querySelector(".drawer .turns");
  if (!row || !turns) return { found: false };
  const rb = row.getBoundingClientRect(), tb = turns.getBoundingClientRect();
  return {
    found: true,
    collapsed: row.classList.contains("collapsed"),
    /* Scroll-follow's job: the row carrying the cursor is on screen. */
    inView: rb.bottom > tb.top && rb.top < tb.bottom
  };
})()`);

// --- manual drag wins, and survives what used to grow the drawer ------------
const gutter = await page.eval(`(() => {
  const g = document.querySelector(".drawer-gutter");
  const b = g.getBoundingClientRect();
  return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) };
})()`);
await mouse("mousePressed", gutter.x, gutter.y);
await mouse("mouseMoved", gutter.x, 700);
await mouse("mouseReleased", gutter.x, 700);
await wait(600);
const draggedTo = (await page.eval(state)).drawerPx;
await wait(6000);
report.drag = { draggedTo, ...(await page.eval(state)) };
report.shots.dragged = await shot("2-dragged");

/* Double-click asks the question again, which is the only way back. */
await page.eval(
  `document.querySelector(".drawer-gutter")?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }))`
);
await wait(900);
report.refitByDoubleClick = await page.eval(state);

// --- manual toggle opens a folded turn --------------------------------------
/* The desk's height around the toggle, because the fit promises the reader asked to see
   a turn and not to have the desk resize under them. */
const deskBefore = (await page.eval(state)).drawerPx;
report.manualToggle = await page.eval(`(() => {
  const folded = [...document.querySelectorAll(".drawer .turn-row.collapsed")].pop();
  if (!folded) return { found: false };
  const before = Math.round(folded.getBoundingClientRect().height);
  folded.querySelector(".turn-toggle")?.click();
  return { found: true, before };
})()`);
await wait(700);
report.manualToggle = {
  ...report.manualToggle,
  deskBefore,
  deskAfter: (await page.eval(state)).drawerPx,
  after: await page.eval(`(() => {
    const rows = [...document.querySelectorAll(".drawer .turn-row")];
    const row = rows.at(-1);
    return { px: Math.round(row.getBoundingClientRect().height), collapsed: row.classList.contains("collapsed") };
  })()`)
};
report.shots.toggled = await shot("3-manual-toggle");

// --- the transport aside, and the now-card it still opens -------------------
await boot(`http://127.0.0.1:${PORT}/?p=graph&p2=latency`);
await pickSession(5);
await wait(18000);
await page.eval(`document.querySelector(".session-anchor[aria-expanded='true']")?.click()`);
await wait(1500);
report.aside = await page.eval(state);
await page.eval(`document.querySelector(".step-controller .now .readout")?.click()`);
await wait(700);
report.nowCard = await page.eval(state);
report.shots.nowCard = await shot("4-now-card");
await page.eval(`document.querySelector(".step-controller .now .readout")?.click()`);
await wait(500);

/* And the toggle still toggles, with its state reading true either way round. */
const press = async () => {
  await page.eval(
    `document.querySelector('.step-controller .aside button[aria-label$="the bottom drawer"]')?.click()`
  );
  await wait(1300);
  return page.eval(state);
};
report.toggleShut = await press();
report.toggleOpen = await press();
report.shots.reopened = await shot("5-reopened");

// --- the rail column form, which also carried the note ----------------------
await boot(`http://127.0.0.1:${PORT}/?p=graph,latency`);
await pickSession(5);
await wait(18000);
await page.eval(`document.querySelector(".session-anchor[aria-expanded='true']")?.click()`);
await wait(1500);
report.railColumn = await page.eval(`(() => ({
  present: !!document.querySelector(".app > .rail section.waterfall"),
  scaleNote: !!document.querySelector(".scale-note, .axis-row"),
  rulers: document.querySelectorAll(".rail .turn-row:not(.collapsed) .axis .tick").length
}))()`);
report.shots.railColumn = await shot("6-rail-column");

// --- and the one fit that is deliberately not snug: the cap biting -----------
/* Tested by shrinking the window rather than by hunting for a session with forty turns:
   50vh of a short window is less than this trace wants, and a double-click on the gutter
   asks for a fresh fit against the new one. Last, and with the metrics put back, so
   nothing above is measured in a window it did not run in. */
await boot(`http://127.0.0.1:${PORT}/?p=graph&p2=latency`);
await pickSession(5);
await wait(18000);
await page.eval(`document.querySelector(".session-anchor[aria-expanded='true']")?.click()`);
await wait(1500);
report.beforeCap = await page.eval(state);
await page.send("Emulation.setDeviceMetricsOverride", {
  width: VIEWPORT.width,
  height: 420,
  deviceScaleFactor: 1,
  mobile: false
});
await wait(900);
await page.eval(
  `document.querySelector(".drawer-gutter")?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }))`
);
await wait(1200);
report.capped = await page.eval(state);
report.shots.capped = await shot("7-capped");
await page.send("Emulation.setDeviceMetricsOverride", { ...VIEWPORT, deviceScaleFactor: 1, mobile: false });

console.log(JSON.stringify(report, null, 2));

/* The load-bearing rules, asserted rather than eyeballed. Everything above is context
   for reading a failure; these are what the fit promises. */
let bad = 0;
const claim = (ok, what) => {
  console.log(`${ok ? "OK" : "XX"} ${what}`);
  if (!ok) bad += 1;
};
const settled = report.coldLoad.samples.slice(-4);
claim(
  Math.max(...report.coldLoad.samples) <= report.coldLoad.capAuto,
  `no unattended fit exceeded the auto cap (max ${Math.max(...report.coldLoad.samples)} ` +
    `of ${report.coldLoad.capAuto}px, hard cap ${report.coldLoad.cap60}px)`
);
claim(
  new Set(settled).size === 1,
  `the fit settles instead of creeping — last four samples ${settled.join(", ")}`
);
claim(
  report.coldLoad.drawerPx > DRAWER_MIN_GUESS,
  `the cold load ended up sized to its trace, not locked tiny (${report.coldLoad.drawerPx}px)`
);
claim(
  report.drag.drawerPx === report.drag.draggedTo,
  `a dragged height wins and survives what follows (${report.drag.draggedTo}px held)`
);
claim(
  report.refitByDoubleClick.drawerPx !== report.drag.draggedTo,
  `double-clicking the gutter asks the question again (${report.refitByDoubleClick.drawerPx}px)`
);
claim(!report.coldLoad.scaleNote && !report.railColumn.scaleNote, "the scale note is gone from both forms");
claim(
  report.coldLoad.rulers > 0 && report.railColumn.rulers > 0,
  "every drawn row still carries its own ruler"
);
claim(report.aside.asideOrder?.chipFirst === true, "the transport aside reads chip first, icon last");
claim(report.toggleShut.drawerPx === 0 && report.toggleOpen.drawerPx > 0, "the transport toggle still shuts and reopens");

/**
 * Snug, not merely bounded.
 *
 * The claims above put the height between a floor and a cap, and a drawer fitted to
 * entirely the wrong trace passes that: anything from 96 to 477px is inside the window,
 * so a fit a row out reads as a pass. What the fit actually promises is the gap under the
 * last row — `DRAWER_FIT_SLACK` plus the drawer's own padding, and nothing else — so that
 * is what to assert, and it holds the same way for a one-turn trace and a twenty-turn one.
 *
 * The bound comes off the measurement, not off taste. Every settled fit here reports 26px;
 * a folded row costs 52 and an expanded one 95, so 48px is the useful ceiling: it leaves
 * 22px for sub-pixel and scrollbar variance while staying under the smallest row, which is
 * the whole point — a tolerance of a row or more cannot tell a snug fit from one row out.
 */
const SNUG_MAX_PX = 48;
for (const [what, snapshot] of [
  ["the cold load", report.coldLoad],
  ["the double-click refit", report.refitByDoubleClick],
  ["the reopened drawer", report.toggleOpen]
]) {
  /* A capped fit is deliberately not snug: content wanted more than 50vh and was told no,
     so the gap under the last row is whatever is left of the cap. Asserting snugness there
     would fail exactly where the cap is working, which is why this is conditional — and why
     the capped case gets a claim of its own below rather than just an exemption. */
  const capped = snapshot.drawerPx >= snapshot.capAuto;
  const gap = snapshot.gapBelowLastPx;
  claim(
    capped || (gap != null && gap >= 0 && gap <= SNUG_MAX_PX),
    capped
      ? `${what} hit the cap, so snugness does not apply (${snapshot.drawerPx}px)`
      : `${what} is fitted snug to its last row (${gap}px under it, at most ${SNUG_MAX_PX})`
  );
}
claim(
  report.capped.drawerPx <= report.capped.capAuto && report.capped.scrolls === true,
  `a fit that wanted more than the cap stops at it and scrolls rather than cutting the ` +
    `trace off (${report.beforeCap.drawerPx}px wanted, ${report.capped.drawerPx}px of ` +
    `${report.capped.capAuto}px given, scrolls ${report.capped.scrolls})`
);
claim(
  report.manualToggle.deskBefore === report.manualToggle.deskAfter,
  `opening a folded turn does not resize the desk (${report.manualToggle.deskBefore}px held)`
);

page.close();
process.exitCode = bad === 0 ? 0 : 1;
