// ABOUTME: Asserts that following the playhead moves the pane's own scroller and nothing else. The
// reported bug was that scrubbing slid the entire pane rail sideways and yanked a log pane the
// reader had scrolled by hand: `scrollIntoView` scrolls every scrollable ANCESTOR, and PaneRail's
// `.rail` is `overflow-x: auto` on purpose. So this pins four things — one write to one element per
// frame, "nearest" semantics so a visible row is never moved, standing down when the reader scrolls
// away, and handing control back when they scroll to the playhead again — and then greps the two
// panes that follow the playhead to make sure neither has grown a scrollIntoView or a focus() call
// back. This is the check that fails when someone reaches for scrollIntoView again.
//   node ui/scripts/check-log-follow.mjs

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import "./libAlias.mjs";

/* The scroller sits 100px down the page and shows 400px of a 4000px list, so a row's
   viewport rect and its place in the content are different numbers — which is the
   whole point, since the bug was about which coordinate space got scrolled. */
const VIEW_TOP = 100;
const VIEW_HEIGHT = 400;
const CONTENT_HEIGHT = 4000;
const ROW_HEIGHT = 40;

/** rAF, collected rather than run, so the check can prove the coalescing. */
let frames = [];
globalThis.requestAnimationFrame = (callback) => frames.push(callback);
function flushFrames() {
  const pending = frames;
  frames = [];
  for (const callback of pending) callback();
  return pending.length;
}

/**
 * One scroller and a list of rows in it, in the two coordinate spaces that matter.
 *
 * Wrapped in a Proxy that records every property written, because "the follower must
 * not move anything but this box" is only half provable by the numbers: the other half
 * is that it never reaches for a second element, and there is no second element here to
 * reach for.
 */
function makeScroller() {
  const writes = [];
  const target = {
    scrollTop: 0,
    clientHeight: VIEW_HEIGHT,
    scrollHeight: CONTENT_HEIGHT,
    getBoundingClientRect: () => ({
      top: VIEW_TOP,
      bottom: VIEW_TOP + VIEW_HEIGHT,
      height: VIEW_HEIGHT
    })
  };
  const scroller = new Proxy(target, {
    set(object, key, value) {
      writes.push(key);
      object[key] = value;
      return true;
    }
  });

  /* `document` exists only so the module can look a row up by id, exactly as it does
     in the browser. A row's viewport position is its content position less however far
     the box has been scrolled — the arithmetic the real layout does. */
  globalThis.document = {
    getElementById(id) {
      const index = Number(id.replace("row-", ""));
      if (!Number.isFinite(index)) return null;
      const contentTop = index * ROW_HEIGHT;
      return {
        getBoundingClientRect: () => ({
          top: VIEW_TOP + contentTop - target.scrollTop,
          bottom: VIEW_TOP + contentTop - target.scrollTop + ROW_HEIGHT,
          height: ROW_HEIGHT
        })
      };
    }
  };

  return { scroller, target, writes };
}

const { scrollFollower } = await import("../src/lib/state/followScroll.ts");

/* --- one write to one element, once per frame ----------------------------- */

{
  const { scroller, target, writes } = makeScroller();
  const follower = scrollFollower(() => scroller);

  /* A pointer drag or a burst of reply_delta frames moves the playhead many times
     before anything is painted. Ten ticks in one frame is one scroll, to the last of
     them — the measured symptom was ten scroll events per tick, the other way round. */
  for (const row of [10, 20, 30, 40, 50, 60, 70, 80, 90, 99]) follower.to(`row-${row}`);
  assert.equal(flushFrames(), 1, "ten playhead moves in one frame must settle once");
  assert.deepEqual(
    writes,
    ["scrollTop"],
    "the only property the follower may write is the scroller's own scrollTop"
  );

  /* Row 99 spans content 3960..4000; the box shows 400px, so its bottom lands on the
     box's bottom edge. `scrollIntoView` with no block would have centred it. */
  assert.equal(
    target.scrollTop,
    99 * ROW_HEIGHT + ROW_HEIGHT - VIEW_HEIGHT,
    "a row below the fold is scrolled up to the bottom edge, not to the middle"
  );
}

/* --- "nearest": a row already on screen is not moved at all --------------- */

{
  const { scroller, target } = makeScroller();
  const follower = scrollFollower(() => scroller);
  target.scrollTop = 1000;

  /* Content 1000..1400 is on screen, so rows 25 through 34 are visible. Every one of
     them must be a no-op — this is what keeps the pane from twitching on every tick
     of a scrub that never leaves the current screenful. */
  for (const row of [25, 30, 34]) {
    follower.to(`row-${row}`);
    flushFrames();
    assert.equal(target.scrollTop, 1000, `row ${row} is already in view and must not move`);
  }

  /* One step above the fold scrolls by exactly one row, not by a screenful. */
  follower.to("row-24");
  flushFrames();
  assert.equal(target.scrollTop, 960, "a row just above the fold scrolls up by one row");
}

/* --- a row taller than the box aligns its top ----------------------------- */

{
  const { scroller, target } = makeScroller();
  const follower = scrollFollower(() => scroller);
  /* The 3876px turn group from the report, and the reason the clamp is a `min`: without
     it, "bring the bottom into view" would scroll thousands of pixels PAST the top of
     the thing being looked at and show its tail. */
  globalThis.document = {
    getElementById: () => ({
      getBoundingClientRect: () => ({
        top: VIEW_TOP + 500 - target.scrollTop,
        bottom: VIEW_TOP + 500 - target.scrollTop + 3876,
        height: 3876
      })
    })
  };
  follower.to("row-tall");
  flushFrames();
  assert.equal(
    target.scrollTop,
    500,
    "a row taller than the box aligns its top; scrolling to its bottom would hide it"
  );
}

/* --- manual action wins, and hands control back --------------------------- */

{
  const { scroller, target } = makeScroller();
  const follower = scrollFollower(() => scroller);

  follower.to("row-50");
  flushFrames();
  const followed = target.scrollTop;
  assert.ok(followed > 0, "sanity: the follower moved to row 50");

  /* The reader scrolls to the top to read something. Same path a wheel gesture takes:
     the scroll happens, then the component's onscroll runs. */
  target.scrollTop = 0;
  follower.handleScroll();
  follower.to("row-60");
  flushFrames();
  assert.equal(target.scrollTop, 0, "a reader who scrolled away keeps the place they chose");

  /* ...and keeps it for as long as they are away, not just for one tick. */
  for (const row of [70, 80, 90]) {
    follower.to(`row-${row}`);
    flushFrames();
  }
  assert.equal(target.scrollTop, 0, "standing down lasts while the playhead is off screen");

  /* Scrolling back to the playhead is what re-arms it. Row 90 spans content
     3600..3640, so parking the box at 3400 puts it on screen. Without this the pane
     would be dead to the playhead until the page was reloaded, which is a worse bug
     than the one being fixed. */
  target.scrollTop = 3400;
  follower.handleScroll();
  /* Followed to a row that is genuinely off screen, so a no-op cannot be mistaken for
     a stand-down: row 91 is still visible from here and proves nothing either way. */
  follower.to("row-10");
  flushFrames();
  assert.equal(
    target.scrollTop,
    10 * ROW_HEIGHT,
    "scrolling back to the playhead must hand following back"
  );

  /* The follower's own writes must never read as the reader taking over, or following
     would switch itself off on the first tick. */
  follower.to("row-10");
  flushFrames();
  follower.handleScroll();
  follower.to("row-20");
  flushFrames();
  /* Row 20 spans content 800..840 and the box is showing 400..800, so nearest brings
     its bottom to the box's bottom edge: 440, not 800. Had the follower read its own
     write as the reader taking over, this would have stayed at 400. */
  assert.equal(
    target.scrollTop,
    840 - VIEW_HEIGHT,
    "the follower's own scroll must not be mistaken for a reader's"
  );
}

/* --- neither pane may reach for scrollIntoView or focus ------------------- */

/* The panes that follow the playhead. Both had the same scrollIntoView call, and both
   scrolled the same rail with it. */
const PANES = [
  "../src/lib/components/agent/TranscriptPanel.svelte",
  "../src/lib/components/flow/LatencyWaterfall.svelte"
];

for (const path of PANES) {
  const source = await readFile(fileURLToPath(new URL(path, import.meta.url)), "utf8");
  const name = path.split("/").pop();

  /* The call form, not the bare word: both panes name `scrollIntoView` in a comment
     explaining why they no longer call it, and a check that forbade saying so would
     be pressure to delete the explanation. */
  assert.doesNotMatch(
    source,
    /scrollIntoView\s*\(/,
    `${name} must not call scrollIntoView: it scrolls every scrollable ancestor, ` +
      `and PaneRail's .rail is one — see followScroll.ts`
  );
  /* Scroll-following must never take the caret. This is the flat rule rather than a
     careful one, because there is no reason for either pane to move focus at all. */
  assert.doesNotMatch(
    source,
    /\.focus\(\)/,
    `${name} must not move keyboard focus while following the playhead`
  );
  assert.match(
    source,
    /scrollFollower\(\(\) => \w+\)/,
    `${name} should follow the playhead through scrollFollower()`
  );
  /* A follower with no onscroll wired up cannot tell it is fighting the reader, and
     the standing-down proved above would never run in the app. */
  assert.match(
    source,
    /onscroll=\{follower\.handleScroll\}/,
    `${name} must hand its scroller's scroll events to the follower`
  );
}

console.log(
  `check-log-follow: coalescing, nearest, tall-row clamp, stand-down and re-arm OK; ` +
    `${PANES.length} panes free of scrollIntoView and focus()`
);
