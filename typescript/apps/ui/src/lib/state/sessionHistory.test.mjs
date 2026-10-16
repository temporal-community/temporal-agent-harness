import assert from "node:assert/strict";
import { describe, it } from "vitest";
import { sessionDay, sessionTitle } from "./sessionHistory";

describe("recognizable session history", () => {
  it("uses the opening message, with useful fallbacks for sessions without one", () => {
    assert.equal(sessionTitle({ initial_user_message: "  Plan a trip\n to Paris ", label: "Session 1" }), "Plan a trip to Paris");
    assert.equal(sessionTitle({ initial_user_message: " ", label: "Imported session" }), "Imported session");
    assert.equal(sessionTitle({}), "Untitled session");
  });

  it("groups by local calendar dates, including across month boundaries", () => {
    const now = new Date(2026, 9, 1, 0, 10);
    assert.equal(sessionDay(new Date(2026, 9, 1, 0, 5).getTime() / 1000, now), "Today");
    assert.equal(sessionDay(new Date(2026, 8, 30, 23, 59).getTime() / 1000, now), "Yesterday");
    assert.notEqual(sessionDay(new Date(2025, 9, 1).getTime() / 1000, now), "Today");
    assert.equal(sessionDay(0, now), "Unknown date");
  });

});
