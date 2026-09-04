/**
 * The transport hotkeys with the scrub slider itself focused.
 *
 * The reported bug is that traversal stops working from the one control a
 * reader is most likely to be standing on. This presses every binding with
 * `input.scrub-input` focused and reads back the playhead, the slider's own
 * value and its `aria-valuetext`, so "swallowed", "worked once" and "moved
 * twice" are three different rows rather than one impression.
 *
 * Run it against a non-scrub focus too, so a key that is broken everywhere is
 * not mistaken for a key the range is eating.
 *
 * Tracked because check-replay-hotkeys.mjs cites this as where its focus claim was
 * measured. Needs a browser binary and a running stack, so it stays out of the suite.
 *
 * Usage: node ui/tools/scrub-focus-keys.mjs [outdir]
 */
import { mkdir, writeFile } from "node:fs/promises";
import { open } from "./scroll-probe/cdp.mjs";

/* Default to the built app on :8000. Point BASE at the vite dev server (:5173) to measure a source
   change before dist is rebuilt — the bug this was written for lives in a module, not in dist. */
const BASE = process.env.BASE ?? "http://127.0.0.1:8000";
const SESSION = "agent-session-96808e3b-c20f-474b-b73b-225e97aa4d4c";
const STORAGE_KEY = "temporal-agent-ui.active-session.v1";
const OUT = process.argv[2] ?? "/tmp/scrub-focus-keys";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const api = await open({ port: 9496, profile: "/tmp/scrub-focus-profile" });
await api.send("Emulation.setDeviceMetricsOverride", {
  width: 1600,
  height: 1000,
  deviceScaleFactor: 2,
  mobile: false
});
await mkdir(OUT, { recursive: true });

/** Wait for the scrubber to exist rather than guess: vite's cold transform is far slower than dist. */
async function waitForScrub(what) {
  for (let i = 0; i < 60; i += 1) {
    const ready = await api.eval(
      `!!document.querySelector('input.scrub-input') && Number(document.querySelector('input.scrub-input').max) > 0`
    );
    if (ready) return;
    await sleep(500);
  }
  throw new Error(`no populated scrubber after 30s (${what})`);
}

await api.send("Page.navigate", { url: `${BASE}/?p=graph,chat` });
await waitForScrub("first load");
await api.eval(
  `localStorage.setItem(${JSON.stringify(STORAGE_KEY)}, ${JSON.stringify(SESSION)}), 1`
);
await api.send("Page.navigate", { url: `${BASE}/?p=graph,chat` });
await waitForScrub("target session");
await sleep(2500);

const VK = {
  ArrowLeft: 37,
  ArrowRight: 39,
  ArrowUp: 38,
  ArrowDown: 40,
  Home: 36,
  End: 35,
  PageUp: 33,
  PageDown: 34,
  " ": 32,
  ",": 188,
  ".": 190,
  f: 70
};
const SHIFT = 8;
const ALT = 1;

async function key(name, modifiers = 0) {
  const isChar = name.length === 1 && name !== " ";
  for (const type of ["rawKeyDown", "keyUp"]) {
    await api.send("Input.dispatchKeyEvent", {
      type: type === "rawKeyDown" && isChar && modifiers === 0 ? "keyDown" : type,
      key: name,
      code: name === " " ? "Space" : isChar ? `Key${name.toUpperCase()}` : name,
      text: type === "rawKeyDown" && isChar && modifiers === 0 ? name : undefined,
      modifiers,
      windowsVirtualKeyCode: VK[name] ?? 0,
      nativeVirtualKeyCode: VK[name] ?? 0
    });
  }
  await sleep(320);
}

const READ = `(() => {
  const scrub = document.querySelector('input.scrub-input');
  const active = document.activeElement;
  return {
    sliderValue: scrub ? Number(scrub.value) : null,
    max: scrub ? Number(scrub.max) : null,
    valueText: scrub ? scrub.getAttribute('aria-valuetext') : null,
    activeIsScrub: active === scrub,
    focusedPane: (document.querySelector('.pane.focused')?.getAttribute('aria-label') || '')
      .split(':')[0] || null,
    playing: document.querySelector('[aria-label*="Pause"]') != null,
    helpOpen: document.querySelector('.sheet') != null,
    bleeding: document.querySelector('.app')?.classList.contains('bleed') ?? false
  };
})()`;
const read = () => api.eval(READ);

/** Park the playhead somewhere with room on both sides, without touching focus. */
async function park(at) {
  await api.eval(`(() => {
    const scrub = document.querySelector('input.scrub-input');
    scrub.value = ${at};
    scrub.dispatchEvent(new Event('input', { bubbles: true }));
    return scrub.value;
  })()`);
  await sleep(400);
}

async function focusScrub() {
  await api.eval(`(document.querySelector('input.scrub-input').focus(), 1)`);
  await sleep(200);
}

async function focusPane() {
  await api.eval(`(() => {
    const pane = [...document.querySelectorAll('.pane')].find((p) =>
      (p.getAttribute('aria-label') || '').startsWith('Session flow') ||
      (p.getAttribute('aria-label') || '').startsWith('State flow'));
    (pane ?? document.querySelector('.pane')).focus();
    return 1;
  })()`);
  await sleep(200);
}

const CASES = [
  { chord: "→", name: "ArrowRight", mods: 0, want: "one event forward" },
  { chord: "←", name: "ArrowLeft", mods: 0, want: "one event back" },
  { chord: "Shift →", name: "ArrowRight", mods: SHIFT, want: "next turn" },
  { chord: "Shift ←", name: "ArrowLeft", mods: SHIFT, want: "previous turn" },
  { chord: ".", name: ".", mods: 0, want: "next step boundary" },
  { chord: ",", name: ",", mods: 0, want: "previous step boundary" },
  { chord: "Home", name: "Home", mods: 0, want: "first event" },
  { chord: "End", name: "End", mods: 0, want: "live edge" },
  { chord: "PageDown", name: "PageDown", mods: 0, want: "native range big step" },
  { chord: "↓", name: "ArrowDown", mods: 0, want: "native range one step" },
  { chord: "Space", name: " ", mods: 0, want: "play / pause" },
  { chord: "Alt →", name: "ArrowRight", mods: ALT, want: "focus the pane right" },
  { chord: "F", name: "f", mods: 0, want: "full-screen the focused pane" }
];

/* Mid-run, so a forward key has somewhere to go. Parking at the live edge makes
   every forward binding look broken for the one reason that is not a bug. */
const { max } = await read();
const PARK = Math.max(1, Math.round(max * 0.45));
console.log(`run has ${max} events; parking each probe at ${PARK}`);

async function sweep(label, focus) {
  console.log(`\n================ ${label} ================`);
  console.log(
    `${"chord".padEnd(9)} ${"want".padEnd(30)} ${"playhead".padEnd(16)} valuetext / note`
  );
  const rows = [];
  for (const testCase of CASES) {
    await park(PARK);
    await focus();
    const before = await read();
    await key(testCase.name, testCase.mods);
    const after = await read();
    const delta = after.sliderValue - before.sliderValue;
    const note = [];
    if (testCase.chord === "Space") note.push(`playing ${before.playing}->${after.playing}`);
    if (testCase.chord === "F") note.push(`bleed ${before.bleeding}->${after.bleeding}`);
    if (testCase.chord === "Alt →") note.push(`pane ${before.focusedPane}->${after.focusedPane}`);
    rows.push({ chord: testCase.chord, delta, after });
    console.log(
      `${testCase.chord.padEnd(9)} ${testCase.want.padEnd(30)} ` +
        `${`${before.sliderValue} -> ${after.sliderValue}`.padEnd(16)} ` +
        `${JSON.stringify(after.valueText)} ${note.join(" ")}`
    );
    /* Undo the two that latch, so the next row starts clean. */
    if (testCase.chord === "Space" && after.playing) await key(" ");
    if (testCase.chord === "F" && after.bleeding) {
      await api.eval(`(document.querySelector('input.scrub-input')?.focus(), 1)`);
      await key("Escape");
    }
  }
  const { data } = await api.send("Page.captureScreenshot", { format: "png" });
  const file = `${OUT}/${label.replace(/\W+/g, "-")}.png`;
  await writeFile(file, Buffer.from(data, "base64"));
  console.log(`shot: ${file}`);
  return rows;
}

const onScrub = await sweep("scrub input focused", focusScrub);
const onPane = await sweep("a pane focused", focusPane);

/* The other side of the same guard. A range is not text entry and must keep the table; a composer
   is, and must keep every key for itself — Option+Left is "back one word" to the OS long before it
   is "pane to the left" here. Both readings come from one `typing` flag, so a fix that widened it
   to unstick the scrubber would show up right here as a caret that stopped moving. */
console.log(`\n================ chat composer focused ================`);
const composer = `document.querySelector('input[aria-label^="Message "]')`;
await park(PARK);
await api.eval(`(() => {
  const el = ${composer};
  /* A replayed session leaves the composer disabled and a disabled input never takes focus, so
     there would be no keydown to guard. Enabled here only to put focus where a live session
     would: the guard reads the tag and the type, neither of which this touches. */
  el.disabled = false;
  el.focus();
  el.value = 'weather in seattle';
  el.setSelectionRange(18, 18);
  return 1;
})()`);
const composerBefore = await read();
await key("ArrowLeft", ALT);
await key("ArrowLeft", ALT);
const composerAfter = await read();
const caret = await api.eval(`(() => {
  const el = ${composer};
  return JSON.stringify({ caret: el.selectionStart ?? null, text: el.value ?? el.textContent });
})()`);
console.log(`Alt ← ×2 in the composer: ${caret}`);
console.log(
  `  playhead ${composerBefore.sliderValue} -> ${composerAfter.sliderValue} ` +
    `(must not move), pane ${composerBefore.focusedPane} -> ${composerAfter.focusedPane} (must not move)`
);
await key("f");
const typedF = await api.eval(`(() => {
  const el = ${composer};
  return JSON.stringify({ text: el.value ?? el.textContent, bleeding: document.querySelector('.app')?.classList.contains('bleed') });
})()`);
console.log(`f in the composer types a letter and does not full-screen: ${typedF}`);
{
  const { data } = await api.send("Page.captureScreenshot", { format: "png" });
  await writeFile(`${OUT}/chat-composer-focused.png`, Buffer.from(data, "base64"));
  console.log(`shot: ${OUT}/chat-composer-focused.png`);
}

console.log("\n================ swallowed by the scrub ================");
for (let i = 0; i < CASES.length; i += 1) {
  const scrubDelta = onScrub[i].delta;
  const paneDelta = onPane[i].delta;
  if (scrubDelta === paneDelta) continue;
  console.log(
    `${CASES[i].chord.padEnd(9)} moves ${paneDelta} from a pane but ${scrubDelta} from the scrub`
  );
}

api.close();
await sleep(600);
