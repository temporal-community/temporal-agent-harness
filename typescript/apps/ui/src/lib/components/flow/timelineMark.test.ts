import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { projectMark } from "./timelineMark";

const span = (startTs: number, durationSeconds: number) => ({ startTs, durationSeconds });

describe("timeline mark geometry", () => {
  it("projects and clamps spans without claiming width for a point", () => {
    assert.deepEqual(projectMark(span(10, 100), 10, 100, 420), {
      leftPct: 0,
      widthPct: 100,
      startPx: 0,
      endPx: 420,
      point: false
    });
    assert.deepEqual(projectMark(span(-10, 20), 0, 100, 420), {
      leftPct: 0,
      widthPct: 20,
      startPx: 0,
      endPx: 84,
      point: false
    });
    assert.deepEqual(projectMark(span(110, 30), 0, 100, 420), {
      leftPct: 99.5,
      widthPct: 0.5,
      startPx: 417.9,
      endPx: 420,
      point: false
    });
    assert.deepEqual(projectMark(span(50, 0), 0, 100, 420), {
      leftPct: 50,
      widthPct: 0,
      startPx: 210,
      endPx: 210,
      point: true
    });
  });

  it("keeps geometry independent of document direction", () => {
    const geometry = projectMark(span(25, 50), 0, 100, 420);
    assert.deepEqual(geometry, {
      leftPct: 25,
      widthPct: 50,
      startPx: 105,
      endPx: 315,
      point: false
    });
  });
});
