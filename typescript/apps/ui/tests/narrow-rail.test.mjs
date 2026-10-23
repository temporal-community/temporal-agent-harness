// ABOUTME: Asserts the pane rail gives way on a narrow screen. Columns have minimum widths of about
// 400 px, so at 390 px (or a 720 px window at 200% zoom) the graph filled the screen and the chat
// sat off-canvas behind a scrollbar, with Send out of reach. Below two panes' width each open column
// must take the whole rail, one at a time, and the rule must win over the column rule that sets the
// width the rest of the time. There is no layout engine here, so this pins the rule itself.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

const source = readFileSync(new URL("../src/lib/panes/PaneRail.svelte", import.meta.url), "utf8");
const media = /@media \(max-width: (\d+)px\)\s*\{/.exec(source);
const rule = media ? source.slice(media.index) : "";

describe("the pane rail on a narrow screen", () => {
  it("applies to a phone and to a 720px window at 200% zoom, and not to a laptop", () => {
    assert.ok(media, "a narrow-screen rule is needed");
    const width = Number(media[1]);
    assert.ok(width >= 390 && width >= 720 / 2, "covers a phone and a zoomed window");
    assert.ok(width < 1024, "leaves a laptop's side-by-side columns alone");
  });

  it("gives every open column the whole rail, with no minimum width, and snaps between them", () => {
    assert.match(rule, /\.rail-slot:not\(\.collapsed\)\s*\{[^}]*flex:\s*0 0 100%;[^}]*min-width:\s*0;/);
    assert.match(rule, /\.rail\s*\{[^}]*scroll-snap-type:\s*x mandatory;/);
  });

  it("is declared after the column rule it must outrank", () => {
    assert.ok(media.index > source.indexOf("\n  .rail-slot {"), "the later rule wins");
  });

  it("leaves folded spines at their own width", () => {
    assert.doesNotMatch(rule, /\.rail-slot\.collapsed/);
  });

  it("shares one breakpoint between the stylesheet and the script that follows it", () => {
    assert.equal(/const NARROW = "\(max-width: (\d+)px\)"/.exec(source)?.[1], media?.[1]);
  });
});
