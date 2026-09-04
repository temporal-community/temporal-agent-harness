/**
 * The digit keys, measured in a browser rather than read off the table.
 *
 *  a) Ctrl+N lands on the Nth column of the rail, from wherever focus was
 *  b) Ctrl+9 is the last column however many there are, not the ninth
 *  c) a digit with no column behind it does nothing at all
 *  d) the drawer is a rail too, so the digits address whichever one was last touched
 *  e) inside the composer, Ctrl+1 does nothing and a bare 1 still types a 1
 *  f) the new rows are in the help overlay, which renders from the same table
 *
 * Lives here rather than in ui/scripts/ because it needs a browser and a running stack,
 * which is what keeps it out of `just app-check`. It is tracked all the same, for the
 * reason the ignore file gives: it can fail, and nine assertions that exit non-zero are
 * worth more to the next reader than a report they have to interpret.
 *
 *   node ui/tools/slot-keys-verify.mjs [port]
 */
import { open } from "./scroll-probe/cdp.mjs";

const BASE = `http://127.0.0.1:${process.argv[2] ?? "5173"}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const api = await open({ port: 9494, profile: "/tmp/slot-keys-profile" });
await api.send("Emulation.setDeviceMetricsOverride", {
  width: 1800,
  height: 1000,
  deviceScaleFactor: 1,
  mobile: false
});

/* Four columns on the main rail and two in the drawer, so "the Nth" and "the last" are
   different answers and there is a second rail to address. */
await api.send("Page.navigate", { url: `${BASE}/?p=graph,chat,logs,latency&p2=logs,latency` });
await sleep(6000);

/* CDP modifier bits: 1 Alt, 2 Ctrl, 4 Meta, 8 Shift. */
const CTRL = 2;

async function key(k, modifiers = 0) {
  const text = k.length === 1 && modifiers === 0 ? k : undefined;
  for (const type of ["keyDown", "keyUp"]) {
    await api.send("Input.dispatchKeyEvent", {
      type,
      key: k,
      code: /^[0-9]$/.test(k) ? `Digit${k}` : k,
      text: type === "keyDown" ? text : undefined,
      modifiers,
      windowsVirtualKeyCode: /^[0-9]$/.test(k) ? 48 + Number(k) : 0,
      nativeVirtualKeyCode: /^[0-9]$/.test(k) ? 48 + Number(k) : 0
    });
  }
  await sleep(350);
}

/* Which column of which rail is focused, read off the DOM the same way a person reads the
   screen: the focused pane's slot carries its index, and the drawer is a rail inside `.drawer`. */
const STATE = `(() => {
  /* Both stacks keep a focused pane of their own, so \`.pane.focused\` has two answers and
     querySelector always gives the rail above. Where DOM focus actually is settles which
     rail the reader is on, and the walk puts it there; the composer case falls back. */
  const focused =
    document.activeElement?.closest?.('.pane') ?? document.querySelector('.pane.focused');
  const slot = focused?.closest('[data-group]');
  const rail = focused?.closest('.drawer') ? 'drawer' : 'main';
  const composer = document.querySelector('.composer input');
  const columnsIn = (sel) =>
    document.querySelectorAll(sel + ' .rail-slot').length;
  return {
    rail: focused ? rail : null,
    column: slot ? Number(slot.getAttribute('data-group')) : null,
    pane: focused?.closest('[data-pane]')?.getAttribute('data-pane') ?? null,
    mainColumns: columnsIn('.rail:not(.drawer .rail)'),
    drawerColumns: document.querySelectorAll('.drawer .rail-slot').length,
    domFocusInPane: document.activeElement?.classList.contains('pane') ?? false,
    composerValue: composer?.value ?? null,
    composerFocused: document.activeElement === composer
  };
})()`;
const read = () => api.eval(STATE);

const results = [];
function record(id, ok, detail) {
  results.push({ id, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${id}  ${detail}`);
}

const opening = await read();
console.log(`\nopened with ${opening.mainColumns} columns on the rail, ${opening.drawerColumns} in the drawer\n`);

// --- (a) Ctrl+N lands on the Nth column ---------------------------------------
{
  const landed = [];
  for (const digit of ["1", "2", "3", "4"]) {
    await key(digit, CTRL);
    landed.push((await read()).column);
  }
  record(
    "a Ctrl+N focuses the Nth column, counted from the left",
    JSON.stringify(landed) === JSON.stringify([0, 1, 2, 3]),
    `Ctrl+1..4 landed on columns ${JSON.stringify(landed)} (want [0,1,2,3])`
  );
  const withFocus = await read();
  record(
    "a the jump takes DOM focus with it, like Alt+Arrows",
    withFocus.domFocusInPane,
    `document.activeElement is the pane: ${withFocus.domFocusInPane}`
  );
}

// --- (b) Ctrl+9 is the last column --------------------------------------------
{
  await key("1", CTRL);
  const before = await read();
  await key("9", CTRL);
  const after = await read();
  record(
    "b Ctrl+9 is the last column, not the ninth",
    after.column === after.mainColumns - 1 && after.column !== before.column,
    `column ${before.column} -> ${after.column} of ${after.mainColumns}`
  );
}

// --- (c) a digit with no column behind it does nothing ------------------------
{
  await key("2", CTRL);
  const before = await read();
  for (const digit of ["5", "6", "7", "8"]) await key(digit, CTRL);
  const after = await read();
  record(
    "c a digit past the end of the rail moves nothing",
    after.column === before.column && after.pane === before.pane,
    `column ${before.column} -> ${after.column}, pane ${before.pane} -> ${after.pane}`
  );
}

// --- (d) the drawer is a rail too ---------------------------------------------
{
  const drawerPane = await api.eval(`(() => {
    const head = document.querySelector('.drawer .pane-head');
    if (!head) return null;
    const r = head.getBoundingClientRect();
    return { x: Math.round(r.x + 30), y: Math.round(r.y + r.height / 2) };
  })()`);
  if (!drawerPane) throw new Error("no drawer rail on the desk — check the ?p2= layout");
  for (const type of ["mousePressed", "mouseReleased"]) {
    await api.send("Input.dispatchMouseEvent", {
      type,
      x: drawerPane.x,
      y: drawerPane.y,
      button: "left",
      clickCount: 1,
      buttons: type === "mousePressed" ? 1 : 0
    });
  }
  await sleep(400);
  await key("2", CTRL);
  const inDrawer = await read();
  record(
    "d with the drawer last touched, the digits address the drawer",
    inDrawer.rail === "drawer" && inDrawer.column === 1,
    `rail=${inDrawer.rail} column=${inDrawer.column} of ${inDrawer.drawerColumns}`
  );
  /* And back: clicking the rail above hands the digits back to it. */
  const mainPane = await api.eval(`(() => {
    const head = document.querySelector('.rail .pane-head');
    const r = head.getBoundingClientRect();
    return { x: Math.round(r.x + 30), y: Math.round(r.y + r.height / 2) };
  })()`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await api.send("Input.dispatchMouseEvent", {
      type,
      x: mainPane.x,
      y: mainPane.y,
      button: "left",
      clickCount: 1,
      buttons: type === "mousePressed" ? 1 : 0
    });
  }
  await sleep(400);
  await key("3", CTRL);
  const backOnMain = await read();
  record(
    "d and the rail above takes them back",
    backOnMain.rail === "main" && backOnMain.column === 2,
    `rail=${backOnMain.rail} column=${backOnMain.column}`
  );
}

// --- (e) the typing guard, both halves ----------------------------------------
{
  const composer = await api.eval(`(() => {
    const input = document.querySelector('.composer input');
    if (!input) return null;
    const r = input.getBoundingClientRect();
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
  })()`);
  if (!composer) throw new Error("no chat composer found");
  for (const type of ["mousePressed", "mouseReleased"]) {
    await api.send("Input.dispatchMouseEvent", {
      type,
      x: composer.x,
      y: composer.y,
      button: "left",
      clickCount: 1,
      buttons: type === "mousePressed" ? 1 : 0
    });
  }
  await sleep(400);
  await api.eval(`(() => {
    const input = document.querySelector('.composer input');
    input.focus();
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return 1;
  })()`);
  const before = await read();
  await key("1", CTRL);
  await key("4", CTRL);
  const afterChord = await read();
  record(
    "e Ctrl+1 in the composer walks no rail",
    afterChord.column === before.column &&
      afterChord.rail === before.rail &&
      afterChord.composerFocused,
    `rail=${before.rail}:${before.column} -> ${afterChord.rail}:${afterChord.column}, ` +
      `composerStillFocused=${afterChord.composerFocused}`
  );
  /* The other half, and the one a guard this broad could easily break: a bare digit is
     still a digit. */
  await key("1");
  await key("2");
  const afterTyping = await read();
  record(
    "e a bare digit still types into the composer",
    afterTyping.composerValue === "12" && afterTyping.column === before.column,
    `composer value ${JSON.stringify(afterTyping.composerValue)} (want "12"), ` +
      `column ${before.column} -> ${afterTyping.column}`
  );
}

// --- (f) written down where a reader can find them ----------------------------
{
  await api.eval(`(() => {
    const input = document.querySelector('.composer input');
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.blur();
    document.querySelector('.pane')?.focus();
    return 1;
  })()`);
  await sleep(300);
  await key("?");
  const listed = await api.eval(`(() => {
    const rows = [...document.querySelectorAll('.sheet .row')];
    return {
      open: document.querySelector('.sheet') != null,
      chords: rows.map((r) => r.querySelector('kbd')?.textContent?.trim()),
      labels: rows.map((r) => r.querySelector('dd')?.textContent?.trim())
    };
  })()`);
  const wanted = ["Ctrl 1", "Ctrl 2", "Ctrl 9"];
  const missing = wanted.filter((chord) => !listed.chords.includes(chord));
  record(
    "f the digit rows are in the help overlay",
    listed.open && missing.length === 0,
    `${listed.chords.length} rows` +
      (missing.length > 0
        ? `; missing ${JSON.stringify(missing)}`
        : `; Ctrl 9 reads "${listed.labels[listed.chords.indexOf("Ctrl 9")]}"`)
  );
  await key("Escape");
}

api.close();
await sleep(600);

const failed = results.filter((r) => !r.ok);
console.log(
  `\n${results.length - failed.length}/${results.length} passed` +
    (failed.length > 0 ? `; failed: ${failed.map((r) => r.id).join(", ")}` : "")
);
process.exit(failed.length > 0 ? 1 : 0);
