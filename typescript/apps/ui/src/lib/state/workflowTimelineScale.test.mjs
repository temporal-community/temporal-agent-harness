import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { formatDuration } from "./replayLog.ts";
import {
  anchoredZoomScroll,
  buildWorkflowScale,
  distinctTickLabels,
  followPlayheadScroll,
  GAP_BREAK_HALF_WIDTH,
  gapBreakX,
  inlineLabelBox,
  projectTime,
  tickLabelBoxes,
  unprojectTime,
  visibleTimeRange,
  visibleTimelineSpans,
  workflowScaleTicks
} from "./workflowTimelineScale.ts";

const closeTo = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9);

describe("workflow timeline scale", () => {
  it("returns a finite origin for empty and zero-duration bounds", () => {
    for (const bounds of [
      { startTs: 0, endTs: 0 },
      { startTs: 10, endTs: 10 },
      { startTs: 10, endTs: 0 }
    ]) {
      const segments = buildWorkflowScale(bounds, [], 0);
      assert.equal(projectTime(segments, bounds.startTs), 0);
      assert.ok(Number.isFinite(unprojectTime(segments, 0)));
    }
    assert.equal(projectTime([], 10), 0);
    assert.equal(unprojectTime([], 10), 0);
  });

  it("projects an ordinary scale linearly and clamps outside the run", () => {
    const segments = buildWorkflowScale({ startTs: 10, endTs: 110 }, [], 500);
    assert.equal(projectTime(segments, 60), 250);
    assert.equal(unprojectTime(segments, 250), 60);
    assert.equal(projectTime(segments, -1), 0);
    assert.equal(projectTime(segments, 1_000), 500);
    assert.equal(unprojectTime(segments, -1), 10);
    assert.equal(unprojectTime(segments, 1_000), 110);
  });

  it("maps a collapsed 50-second gap to exactly 48 pixels", () => {
    const segments = buildWorkflowScale(
      { startTs: 0, endTs: 100 },
      [{ key: "idle", startTs: 25, endTs: 75 }],
      1_000
    );
    const gap = segments.find(({ collapsed }) => collapsed);
    assert.equal(gap.endPx - gap.startPx, 48);
    assert.equal(projectTime(segments, 75) - projectTime(segments, 25), 48);
  });

  it("projects and unprojects through a leading collapsed gap", () => {
    const segments = buildWorkflowScale(
      { startTs: 0, endTs: 100 },
      [{ key: "leading", startTs: 0, endTs: 60 }],
      400
    );
    for (const timestamp of [0, 20, 59.999, 60, 80, 100]) {
      closeTo(unprojectTime(segments, projectTime(segments, timestamp)), timestamp);
    }
    assert.equal(projectTime(segments, 60), 48);
  });

  it("merges overlapping and touching collapsed ranges without false slivers", () => {
    const segments = buildWorkflowScale(
      { startTs: 0, endTs: 100 },
      [
        { key: "first", startTs: 20, endTs: 40 },
        { key: "overlap", startTs: 30, endTs: 60 },
        { key: "touch", startTs: 60, endTs: 80 }
      ],
      500
    );
    assert.equal(segments.filter(({ collapsed }) => collapsed).length, 1);
    assert.equal(segments.length, 3);
    assert.equal(projectTime(segments, 80) - projectTime(segments, 20), 48);
  });

  it("round-trips collapsed boundaries and representative pixels", () => {
    const segments = buildWorkflowScale(
      { startTs: 0, endTs: 200 },
      [{ key: "idle", startTs: 50, endTs: 100 }],
      1_000
    );
    for (const timestamp of [49.999, 50, 50.001, 99.999, 100, 100.001]) {
      closeTo(unprojectTime(segments, projectTime(segments, timestamp)), timestamp);
    }
    for (const px of [0, 100, 317.333, 500, 999, 1_000]) {
      closeTo(projectTime(segments, unprojectTime(segments, px)), px);
    }
  });

  it("keeps two gaps monotonic and finite with a zero-width viewport", () => {
    const segments = buildWorkflowScale(
      { startTs: 0, endTs: 100 },
      [
        { key: "first", startTs: 10, endTs: 20 },
        { key: "second", startTs: 70, endTs: 90 }
      ],
      0
    );
    const projected = [0, 10, 20, 50, 70, 90, 100].map((timestamp) =>
      projectTime(segments, timestamp)
    );
    assert.ok(projected.every(Number.isFinite));
    assert.deepEqual(projected, projected.toSorted((a, b) => a - b));
    assert.deepEqual(visibleTimeRange(segments, 0, 0, 0), [0, 0]);
  });

  it("keeps gaps fixed while zoom changes expanded widths", () => {
    const gap = { key: "idle", startTs: 25, endTs: 75 };
    const fit = buildWorkflowScale({ startTs: 0, endTs: 100 }, [gap], 500);
    const zoomed = buildWorkflowScale({ startTs: 0, endTs: 100 }, [gap], 1_000);
    closeTo(fit[1].endPx - fit[1].startPx, 48);
    closeTo(zoomed[1].endPx - zoomed[1].startPx, 48);
    assert.ok(zoomed[0].endPx - zoomed[0].startPx > fit[0].endPx - fit[0].startPx);
  });

  it("restores proportional width when a user expands a gap", () => {
    const collapsed = buildWorkflowScale(
      { startTs: 0, endTs: 100 },
      [{ key: "idle", startTs: 25, endTs: 75 }],
      1_000
    );
    const expanded = buildWorkflowScale({ startTs: 0, endTs: 100 }, [], 1_000);
    assert.equal(projectTime(collapsed, 75) - projectTime(collapsed, 25), 48);
    assert.equal(projectTime(expanded, 75) - projectTime(expanded, 25), 500);
  });

  it("returns the overscanned visible time range", () => {
    const segments = buildWorkflowScale({ startTs: 0, endTs: 100 }, [], 1_000);
    const [startTs, endTs] = visibleTimeRange(segments, 200, 300, 50);
    closeTo(startTs, 15);
    closeTo(endTs, 55);
  });

  it("keeps the time under the pointer fixed while zooming", () => {
    const fit = buildWorkflowScale({ startTs: 0, endTs: 100 }, [], 500);
    const zoomed = buildWorkflowScale({ startTs: 0, endTs: 100 }, [], 2_000);
    const scrollLeft = anchoredZoomScroll(fit, zoomed, 0, 125, 500, 2_000);
    assert.equal(scrollLeft, 375);
    assert.equal(unprojectTime(zoomed, scrollLeft + 125), 25);
  });

  it("binary-searches only spans intersecting the visible time range", () => {
    const spans = Array.from({ length: 100_000 }, (_, index) => ({
      startTs: index * 2,
      endTs: index * 2 + 1
    }));
    assert.deepEqual(visibleTimelineSpans(spans, 80_001, 80_006), spans.slice(40_000, 40_004));
  });

  it("follows the playhead inside gutters unless direct manipulation suspends it", () => {
    assert.equal(followPlayheadScroll(950, 0, 1_000, 4_000), 100);
    assert.equal(followPlayheadScroll(50, 500, 1_000, 4_000), 0);
    assert.equal(followPlayheadScroll(950, 0, 1_000, 4_000, true), 0);
  });

  it("spaces ticks across active segments and omits collapsed spans", () => {
    const segments = buildWorkflowScale(
      { startTs: 0, endTs: 100 },
      [{ key: "idle", startTs: 10, endTs: 90 }],
      500
    );
    const ticks = workflowScaleTicks(segments);
    assert.deepEqual(ticks, [0, 5, 10, 90, 95, 100]);
    assert.ok(ticks.every((tick) => tick <= 10 || tick >= 90));
    const distances = ticks
      .slice(1)
      .map((tick, index) => projectTime(segments, tick) - projectTime(segments, ticks[index]))
      .filter((distance) => distance > 48);
    assert.ok(distances.every((distance) => distance >= 60));
  });

  it("prints a label once when the ticks either side of a gap round to it", () => {
    const segments = buildWorkflowScale(
      { startTs: 0, endTs: 5 },
      [{ key: "idle", startTs: 2.2, endTs: 2.4 }],
      500
    );
    const label = (tick) => formatDuration(tick);
    const raw = workflowScaleTicks(segments);
    const rawLabels = raw.map(label);
    assert.notDeepEqual(rawLabels, [...new Set(rawLabels)], `the scale alone repeats a label: ${rawLabels.join(", ")}`);
    const labels = distinctTickLabels(raw, label).map(label);
    assert.deepEqual(labels, [...new Set(labels)], `"2s ≀ 2s" is gone: ${labels.join(", ")}`);
    assert.equal(labels[0], "0s");
    assert.equal(labels.at(-1), "5s");

    assert.deepEqual(distinctTickLabels([0, 0.2, 5, 9.8, 10], label), [0, 5, 10], "the end tick stays");
    assert.deepEqual(distinctTickLabels([], label), []);
  });

  it("leaves a label's width between the end tick and the tick before it", () => {
    const segments = buildWorkflowScale({ startTs: 0, endTs: 12_250 }, [], 1_219);
    const ticks = workflowScaleTicks(segments);
    assert.equal(ticks.at(-1), 12_250);
    const endPx = projectTime(segments, 12_250);
    assert.ok(endPx - projectTime(segments, ticks.at(-2)) >= 96);
  });

  /* 6px a character is wider than the axis's 10px tabular digits, so the estimate errs wide. */
  const estimate = (label) => (tick) => label(tick).length * 6;
  const breakRange = (segment) => [gapBreakX(segment) - GAP_BREAK_HALF_WIDTH, gapBreakX(segment) + GAP_BREAK_HALF_WIDTH];
  const clear = (box, [left, right]) => box.right <= left || box.left >= right;

  it("keeps the end label off the last break when a fold leaves under 30px after it", () => {
    const segments = buildWorkflowScale(
      { startTs: 0, endTs: 12_253 },
      [{ key: "idle", startTs: 1_400, endTs: 12_250 }],
      800
    );
    const fold = segments.find((segment) => segment.collapsed);
    assert.ok(800 - fold.endPx < 30, `${800 - fold.endPx}px after the fold`);
    const label = (tick) => formatDuration(tick);
    const ticks = distinctTickLabels(workflowScaleTicks(segments), label);
    const boxes = tickLabelBoxes(segments, ticks, estimate(label));
    const end = boxes.at(-1);
    assert.equal(end.tick, 12_253, "the run's total time stays");
    assert.ok(clear(end, breakRange(fold)), `end label ${end.left}-${end.right} clears the ≀ at ${breakRange(fold)}`);
    const previous = boxes.at(-2);
    assert.ok(previous.right <= end.left, `previous label ends at ${previous.right}, end starts at ${end.left}`);
    for (const box of boxes.slice(0, -1)) {
      for (const segment of segments.filter((item) => item.collapsed)) assert.ok(clear(box, breakRange(segment)));
    }
  });

  it("keeps the start label off a leading fold's break", () => {
    const segments = buildWorkflowScale(
      { startTs: 0, endTs: 10_000 },
      [{ key: "idle", startTs: 0, endTs: 9_000 }],
      800
    );
    const label = () => "start label";
    const ticks = workflowScaleTicks(segments);
    const boxes = tickLabelBoxes(segments, ticks, estimate(label));
    const fold = segments[0];
    assert.equal(boxes[0].tick, 0);
    assert.ok(clear(boxes[0], breakRange(fold)), `start label ${boxes[0].left} clears the ≀`);
    assert.ok(boxes[0].right <= boxes[1].left);
  });

  it("drops a label that would overlap the one before it between two close folds", () => {
    // ?mock=run100k at a 320px pane: "2h 34m" hung out from one break ran into "4m 34s" hung back from the next.
    const segments = buildWorkflowScale(
      { startTs: 0, endTs: 40_000 },
      [{ key: "a", startTs: 1_000, endTs: 9_000 }, { key: "b", startTs: 9_300, endTs: 30_000 }],
      300
    );
    const label = (tick) => formatDuration(tick);
    const boxes = tickLabelBoxes(segments, distinctTickLabels(workflowScaleTicks(segments), label), estimate(label));
    for (let index = 1; index < boxes.length; index += 1) {
      assert.ok(boxes[index - 1].right <= boxes[index].left, `${boxes[index - 1].right} runs into ${boxes[index].left}`);
    }
  });
});

describe("a narrow pane's inline span names", () => {
  // A 400px plot, caps 100–150, a 60px name of ten characters and a 6px gap.
  const span = (overrides = {}) => ({
    outer: [100, 150], plotWidth: 400, breaks: [], width: 60, chars: 10, gap: 6, rtl: false, ...overrides
  });

  it("goes after the span's last cap when it fits", () => {
    assert.deepEqual(inlineLabelBox(span()), { side: "after", left: 156, width: 60, fade: false, alignRight: false });
  });

  it("goes before the first cap, set against it, when the end has no room", () => {
    assert.deepEqual(inlineLabelBox(span({ outer: [100, 380] })), {
      side: "before", left: 34, width: 60, fade: false, alignRight: true
    });
  });

  it("goes on a solid lane only when neither side has room", () => {
    const long = { outer: [10, 395], lane: [32, 373] };
    assert.equal(inlineLabelBox(span(long)).side, "inside");
    assert.equal(inlineLabelBox(span({ outer: [10, 395] })), null, "no lane, no room");
  });

  it("fades in the widest room when it is a little short, and drops under three characters", () => {
    assert.deepEqual(inlineLabelBox(span({ outer: [20, 360] })), {
      side: "after", left: 366, width: 34, fade: true, alignRight: false
    });
    assert.equal(inlineLabelBox(span({ outer: [10, 390] })), null, "4px each side holds under three of ten characters");
    assert.equal(inlineLabelBox(span({ outer: [10, 390], width: 6, chars: 1 })), null, "a short name shows whole or not at all");
  });

  it("stops short of a gap break past the span", () => {
    const box = inlineLabelBox(span({ breaks: [200] }));
    assert.equal(box.side, "before", "the break leaves 37px after the span");
    assert.equal(inlineLabelBox(span({ breaks: [200, 60] })).side, "after", "a fade in the wider room");
  });

  it("mirrors the sides right to left", () => {
    assert.equal(inlineLabelBox(span({ rtl: true })).side, "before");
    assert.equal(inlineLabelBox(span({ rtl: true, outer: [20, 150] })).side, "after");
    const inside = inlineLabelBox(span({ rtl: true, outer: [10, 395], lane: [32, 373] }));
    assert.deepEqual([inside.side, inside.alignRight, inside.left + inside.width], ["inside", true, 367]);
  });
});
