// ABOUTME: Pins the one timestamp formatter the chat and the log pane print every row's time with.
// It has to read exactly as the per-row toLocaleTimeString it replaced, build its Intl formatter
// once rather than once per row, and print nothing — not throw — for a timestamp that is not one.

import assert from "node:assert/strict";
import { describe, it, vi } from "vitest";

import { formatTimestamp } from "./replayLog.ts";

describe("formatTimestamp", () => {
  it("reads as toLocaleTimeString did, from one formatter", () => {
    const built = vi.spyOn(Intl, "DateTimeFormat");
    const seconds = 1_700_000_000.25;
    const expected = new Date(seconds * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
    for (let row = 0; row < 200; row += 1) assert.equal(formatTimestamp(seconds), expected);
    assert.equal(built.mock.calls.length, 1, "a formatter was built per row");
    built.mockRestore();
  });

  it("prints nothing for a timestamp that is not a number", () => {
    assert.equal(formatTimestamp(Number.NaN), "");
  });
});
