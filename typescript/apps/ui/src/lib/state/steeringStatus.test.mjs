// Steering a message queues for the open turn is shown on that message: queued, then sent to the
// model with the tool result that carried it, or dropped when the turn ended first. The claim and
// the drop name what they settle by `steering_ids`, not on the envelope, so a fold keyed on the
// envelope's message would settle the wrong one, or none.
//   npx vitest run src/lib/state/steeringStatus.test.mjs
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { scaleScenario } from "../mock/scaleScenarios.ts";
import { buildReplayLog, rowIdentifiers } from "./replayLog.ts";
import { buildTranscript } from "./transcript.ts";

const { frames } = scaleScenario("steering");
const users = (items) => items.filter((item) => item.kind === "user");
const upTo = (event, nth = 1) => {
  let seen = 0;
  return frames.slice(0, frames.findIndex((f) => f.event === event && ++seen === nth) + 1);
};

describe("a steering message in the transcript", () => {
  it("is queued once its handler steers", () => {
    const steer = users(buildTranscript(upTo("steering_enqueued")))[1];
    assert.deepEqual(steer.steering, [{ steeringId: "steering-1", text: "Morning departures only", status: "enqueued", toolId: null }]);
  });

  it("is claimed with the tool call whose result carried it, though the claiming message is another", () => {
    const [ask, steer] = users(buildTranscript(upTo("steering_claimed")));
    assert.equal(ask.steering, undefined, "the message running the model loop steered nothing");
    assert.deepEqual(steer.steering, [
      { steeringId: "steering-1", text: "Morning departures only", status: "claimed", toolId: "tool-steer-search" }
    ]);
  });

  it("is dropped when the turn ends first, leaving the claimed one as it was", () => {
    const [, first, second] = users(buildTranscript(frames));
    assert.equal(first.steering[0].status, "claimed");
    assert.deepEqual(second.steering, [{ steeringId: "steering-2", text: "Window seat, please", status: "dropped", toolId: null }]);
  });

  it("leaves a message that never steered without the field", () => {
    const transcript = buildTranscript(frames.filter((f) => !f.event.startsWith("steering_")));
    for (const item of users(transcript)) assert.equal("steering" in item, false);
  });
});

describe("steering in the log", () => {
  const rows = buildReplayLog(frames).rows.filter((row) => row.event.startsWith("steering_"));

  it("has a row for each event, the claim filed under the tool call that carried it", () => {
    assert.deepEqual(
      rows.map((row) => [row.event, row.label, row.toolId]),
      [
        ["steering_enqueued", "Steering queued", undefined],
        ["steering_claimed", "Steering sent to the model", "tool-steer-search"],
        ["steering_enqueued", "Steering queued", undefined],
        ["steering_dropped", "Steering dropped", undefined]
      ]
    );
    assert.equal(rows[0].body, "Morning departures only");
  });

  it("flags only the drop on the scrubber, and never as a failure", () => {
    assert.deepEqual(rows.map((row) => row.marker), [undefined, undefined, undefined, "queue"]);
    assert.ok(rows.every((row) => row.tone !== "error"));
  });

  it("keeps each row's steering ids, so a claim names exactly what it claimed", () => {
    assert.deepEqual(rows.map((row) => row.steeringIds), [["steering-1"], ["steering-1"], ["steering-2"], ["steering-2"]]);
    const steeringIds = (row) => rowIdentifiers(row).filter((id) => id.label.includes("steering"));
    assert.deepEqual(steeringIds(rows[0]), [{ label: "steering", value: "steering-1" }]);
    assert.deepEqual(steeringIds(rows[1]), [{ label: "claimed steering 1 of 1", value: "steering-1" }]);
    assert.deepEqual(steeringIds(rows[3]), [{ label: "dropped steering 1 of 1", value: "steering-2" }]);
  });

  it("keeps the claim exactly as it arrived, with nothing the console made up", () => {
    const claim = frames.find((f) => f.event === "steering_claimed");
    assert.deepEqual(rows[1].wire, [claim.data]);
    assert.equal("body" in rows[1].wire[0], false);
    assert.equal("status" in rows[1].wire[0], false);
  });

  it("labels each id a claim settles as one of a list", () => {
    const claim = { ...rows[1], steeringIds: ["a", "b"] };
    assert.deepEqual(
      rowIdentifiers(claim).filter((id) => id.label.includes("steering")).map((id) => id.label),
      ["claimed steering 1 of 2", "claimed steering 2 of 2"]
    );
  });

  it("names the steering message on the row that queued it", () => {
    assert.deepEqual(rowIdentifiers(rows[0]).find((id) => id.label === "message"), { label: "message", value: "msg-steer-1" });
  });
});
