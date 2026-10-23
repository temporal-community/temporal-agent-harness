import assert from "node:assert/strict";
import { afterEach, describe, it, vi } from "vitest";
import { DEFAULT_TEMPORAL_UI, everyMinute, knownStart, sessionAge, sessionDay, sessionTitle, temporalWorkflowUrl } from "./sessionHistory";

describe("a session's age", () => {
  const now = Date.UTC(2026, 9, 20, 12, 0, 0);
  const ago = (seconds) => now / 1000 - seconds;

  it("is one short token per unit, and words for a screen reader", () => {
    const cases = [
      [0, "now"],
      [59, "now"],
      [60, "1m"],
      [119 * 60, "1h"],
      [23 * 3600 + 3599, "23h"],
      [86_400, "1d"],
      [6 * 86_400, "6d"],
      [7 * 86_400, "1w"],
      [51 * 7 * 86_400, "51w"],
      [365 * 86_400, "1y"],
      [3 * 365 * 86_400, "3y"]
    ];
    for (const [seconds, short] of cases) assert.equal(sessionAge(ago(seconds), now).short, short, `${seconds}s`);
    assert.equal(sessionAge(ago(120), now).long, "2 minutes ago");
    assert.equal(sessionAge(ago(86_400), now).long, "yesterday");
    assert.equal(sessionAge(ago(0), now).long, "now");
  });

  it("is blank for a missing, unreadable or future time, and forgives a little clock skew", () => {
    const blank = { short: "", long: "" };
    assert.deepEqual(sessionAge(0, now), blank);
    assert.deepEqual(sessionAge(Number.NaN, now), blank);
    assert.deepEqual(sessionAge(ago(60), Number.NaN), blank);
    assert.deepEqual(sessionAge(Date.UTC(2099, 11, 31) / 1000, now), blank);
    assert.equal(sessionAge(ago(-30), now).short, "now");
  });
});

describe("the minute clock", () => {
  afterEach(() => vi.useRealTimers());

  it("ticks on each minute boundary, as one interval, until stopped", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.UTC(2026, 9, 20, 12, 0, 45));
    const ticks = [];
    const stop = everyMinute((ms) => ticks.push(new Date(ms).getUTCSeconds()));
    vi.advanceTimersByTime(14_999);
    assert.deepEqual(ticks, [], "not before the boundary");
    vi.advanceTimersByTime(1);
    vi.advanceTimersByTime(120_000);
    assert.deepEqual(ticks, [0, 0, 0]);
    assert.equal(vi.getTimerCount(), 1, "one timer, however many rows read it");
    stop();
    assert.equal(vi.getTimerCount(), 0);
  });
});

describe("recognizable session history", () => {
  it("uses the opening message, with useful fallbacks for sessions without one", () => {
    assert.equal(sessionTitle({ initial_user_message: "  Plan a trip\n to Paris ", label: "Session 1" }), "Plan a trip to Paris");
    assert.equal(sessionTitle({ initial_user_message: " ", label: "Imported session" }), "Imported session");
    assert.equal(sessionTitle({}), "Untitled session");
  });

  it("reads a markdown opening message as its plain text", () => {
    assert.equal(sessionTitle({ initial_user_message: "Compare **Berlin** and `Lisbon` for the [offsite](https://example.com)" }), "Compare Berlin and Lisbon for the offsite");
    assert.equal(sessionTitle({ initial_user_message: "<script>alert(1)</script> **bold** # heading" }), "<script>alert(1)</script> bold # heading");
  });

  it("groups by local calendar dates, including across month boundaries", () => {
    const now = new Date(2026, 9, 1, 0, 10);
    assert.equal(sessionDay(new Date(2026, 9, 1, 0, 5).getTime() / 1000, now), "Today");
    assert.equal(sessionDay(new Date(2026, 8, 30, 23, 59).getTime() / 1000, now), "Yesterday");
    assert.notEqual(sessionDay(new Date(2025, 9, 1).getTime() / 1000, now), "Today");
    assert.equal(sessionDay(0, now), "Unknown date");
  });

  /* A start of 86,400 read "56y" under a "Jan 1, 1970" group. */
  it("treats a placeholder start before 2001 as unknown: no age, no 1970 group", () => {
    const now = Date.UTC(2026, 9, 20, 12);
    assert.equal(sessionDay(86_400, new Date(now)), "Unknown date");
    assert.deepEqual(sessionAge(86_400, now), { short: "", long: "" });
    assert.equal(knownStart(999_999_999), false);
    assert.equal(knownStart(1_000_000_000), true);
    assert.equal(knownStart(Number.POSITIVE_INFINITY), false);
    assert.equal(sessionAge(now / 1000 - 120, now).short, "2m", "a real start still ages");
  });

  it("links a session to its workflow in the Temporal Web UI, never through a script URL", () => {
    assert.equal(temporalWorkflowUrl("wf 1/a"), `${DEFAULT_TEMPORAL_UI}/wf%201%2Fa`);
    assert.equal(
      temporalWorkflowUrl("wf", "?temporal-ui=http%3A%2F%2Flocalhost%3A8234%2Fnamespaces%2Fh%2Fworkflows%2F"),
      "http://localhost:8234/namespaces/h/workflows/wf"
    );
    assert.equal(temporalWorkflowUrl("wf", "?temporal-ui=javascript:alert(1)//"), `${DEFAULT_TEMPORAL_UI}/wf`);
    assert.equal(temporalWorkflowUrl("wf", "?temporal-ui=not a url"), `${DEFAULT_TEMPORAL_UI}/wf`);
  });

  it("links through the workflows page the API reports, unless the address bar names one", () => {
    const reported = "http://localhost:8234/namespaces/temporal-agent-harness/workflows";
    assert.equal(temporalWorkflowUrl("wf", "", reported), `${reported}/wf`);
    assert.equal(
      temporalWorkflowUrl("wf", "?temporal-ui=https%3A%2F%2Fcloud.example%2Fnamespaces%2Fn%2Fworkflows", reported),
      "https://cloud.example/namespaces/n/workflows/wf"
    );
    assert.equal(temporalWorkflowUrl("wf", "", "javascript:alert(1)//"), `${DEFAULT_TEMPORAL_UI}/wf`);
    assert.equal(temporalWorkflowUrl("wf", "", null), `${DEFAULT_TEMPORAL_UI}/wf`);
  });

});
