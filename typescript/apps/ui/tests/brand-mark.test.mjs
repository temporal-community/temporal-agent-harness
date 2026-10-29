// ABOUTME: Asserts the Temporal mark drawn in the minimap center: same artwork as
// public/temporal-logo.svg, currentColor fill only, and aria-hidden (decoration).
//
// There are two copies of the mark path on purpose and neither can go. The file has to
// stay because the packaging tests read it off the built dist and out of the wheel — it
// is how they prove public/ was copied at all — and an <img> pointed at it cannot be
// handed a colour, which is the one thing the status row needs from the mark. So the
// strip draws its own copy and takes its ink from a token. Two copies of one path is
// the drift this check exists to make loud: the first person to redraw the mark touches
// one of them, and this says which one they missed.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { describe, it } from "vitest";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");

const [asset, minimap] = await Promise.all([
  read("../public/temporal-logo.svg"),
  read("../src/lib/panes/PaneMinimap.svelte")
]);

const pathData = (text, source) => {
  const found = [...text.matchAll(/\sd="(M[^"]+)"/g)].map((match) => match[1]);
  assert.equal(found.length, 1, `${source} should hold exactly one path, found ${found.length}`);
  return found[0];
};

const markAt = minimap.indexOf('<svg class="brand-mark"');
const markSvg = minimap.slice(markAt, minimap.indexOf("</svg>", markAt) + "</svg>".length);

describe("the mark drawn in the minimap", () => {
  it("is the same artwork as public/temporal-logo.svg", () => {
    assert.notEqual(markAt, -1, "PaneMinimap must draw an svg.brand-mark");

    assert.equal(
      pathData(markSvg, "the minimap's brand-mark"),
      pathData(asset, "public/temporal-logo.svg"),
      "the mark drawn in PaneMinimap has drifted from public/temporal-logo.svg"
    );
  });

  it("takes its ink from a token, and says nothing", () => {
    assert.match(
      markSvg,
      /fill="currentColor"/,
      "the drawn mark must take its fill from currentColor"
    );
    assert.doesNotMatch(
      markSvg,
      /#[0-9a-fA-F]{3,8}\b/,
      "the drawn mark must not name a colour of its own"
    );
    assert.match(
      markSvg,
      /aria-hidden="true"/,
      "the mark is decoration and must be hidden from a reader"
    );
    assert.equal(
      (minimap.match(/Temporal Agent Harness/g) ?? []).length,
      0,
      "PaneMinimap must not speak the product name; the document title does"
    );
  });
});
