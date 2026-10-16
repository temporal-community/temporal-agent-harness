// ABOUTME: Pins the grouping rule behind collapsed reply ticks — which chunks fold into one run
// and, just as load-bearing, which ones must not. The dangerous direction is over-merging: two
// concurrent mid-turn handlers stream under ONE turn_id, so a key of turn_id would silently fuse
// two participants' replies into a single tick and lose the fact that there were two.
//   npx vitest run src/lib/state/replyRuns.test.mjs
//
// Also pins the live property the whole feature rests on: a run is built by folding forwards, so
// the runs over a prefix of the frames must be the runs over all of them, truncated. That is what
// lets one derivation serve both the live edge (partial run, still growing) and replay (the same
// run, complete) without either path carrying its own version of the rule.
import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildReplyRuns, replyRunAt } from "./replyRuns.ts";

const base = {
  agent_id: "7f3c1a",
  turn_id: "turn-001",
  turn_number: 1,
  resume_offset: 0
};

let clock = 1_700_000_000;

function delta(text, overrides = {}) {
  clock += 1;
  return {
    event: "reply_delta",
    data: {
      ...base,
      type: "reply_delta",
      message_id: "msg-001",
      timestamp: clock,
      text,
      ...overrides
    }
  };
}

function toolStart() {
  clock += 1;
  return {
    event: "tool_start",
    data: {
      ...base,
      type: "tool_start",
      message_id: "msg-001",
      timestamp: clock,
      tool_id: "call_1",
      tool_name: "search",
      tool_input: {}
    }
  };
}

describe("buildReplyRuns", () => {
  it("consecutive chunks of one message are one run, carrying all of the text", () => {
    const runs = buildReplyRuns([delta("Use "), delta("local "), delta("activities.")]);

    assert.equal(runs.length, 1, "three chunks of one reply are one event");
    assert.equal(runs[0].text, "Use local activities.");
    assert.equal(runs[0].frameCount, 3);
    assert.equal(runs[0].startIndex, 1, "indices are 1-based, to match viewIndex");
    assert.equal(runs[0].endIndex, 3);
    assert.ok(runs[0].endTs > runs[0].startTs, "a run spans the time its chunks took");
  });

  it("a non-reply frame between chunks splits them", () => {
    const runs = buildReplyRuns([delta("before "), toolStart(), delta("after")]);

    assert.equal(runs.length, 2, "a tool call between two chunks is something that happened");
    assert.deepEqual(
      runs.map((run) => [run.startIndex, run.endIndex, run.text]),
      [
        [1, 1, "before "],
        [3, 3, "after"]
      ]
    );
  });

  // The whole reason the key is the message and not the turn.
  it("two messages streaming under one turn stay two runs, even interleaved", () => {
    const runs = buildReplyRuns([
      delta("first-a ", { message_id: "msg-a" }),
      delta("second-a ", { message_id: "msg-b" }),
      delta("first-b", { message_id: "msg-a" })
    ]);

    assert.equal(
      runs.length,
      3,
      "keyed by turn_id these three collapse into one tick and the run reads as a single reply"
    );
    assert.deepEqual(runs.map((run) => run.text), ["first-a ", "second-a ", "first-b"]);
  });

  it("a subagent's chunks never join the parent's", () => {
    const runs = buildReplyRuns([
      { frame: delta("parent "), workflowId: "root" },
      { frame: delta("child"), workflowId: "root-child" }
    ]);

    assert.equal(runs.length, 2, "both ride the root log; only the workflow tells them apart");
  });

  it("a single chunk is a run of one", () => {
    const runs = buildReplyRuns([delta("Done.")]);

    assert.equal(runs.length, 1);
    assert.equal(runs[0].frameCount, 1);
    assert.equal(runs[0].startIndex, runs[0].endIndex, "it opens and closes on the same frame");
  });

  it("empty and whitespace chunks fold in rather than breaking the run", () => {
    const runs = buildReplyRuns([delta("a"), delta(""), delta(" "), delta("b")]);

    assert.equal(runs.length, 1, "an empty chunk is still part of the reply it arrived in");
    assert.equal(runs[0].text, "a b");
    assert.equal(runs[0].frameCount, 4);
  });

  it("a client stream-error frame closes the open run", () => {
    const runs = buildReplyRuns([
      delta("before "),
      { event: "error", data: { message: "stream dropped" } },
      delta("after")
    ]);

    assert.equal(runs.length, 2, "a frame with no event type must not be folded into a reply");
  });

  it("frames with no reply at all produce no runs", () => {
    assert.deepEqual(buildReplyRuns([toolStart(), toolStart()]), []);
  });

  // The live/replay agreement, stated as the property rather than as two fixtures.
  it("appending a chunk extends the last run: every prefix agrees with the whole", () => {
    const frames = [
      delta("one "),
      toolStart(),
      delta("two "),
      delta("three "),
      delta("four"),
      toolStart()
    ];
    const whole = buildReplyRuns(frames);

    for (let length = 1; length <= frames.length; length += 1) {
      const prefix = buildReplyRuns(frames.slice(0, length));
      assert.deepEqual(
        prefix,
        whole
          .filter((run) => run.startIndex <= length)
          .map((run) =>
            run.endIndex <= length
              ? run
              : /* still streaming at this cursor: same run, truncated to what has arrived */
                {
                  ...run,
                  endIndex: length,
                  frameCount: length - run.startIndex + 1,
                  text: frames
                    .slice(run.startIndex - 1, length)
                    .map((item) => item.data.text)
                    .join(""),
                  endTs: frames[length - 1].data.timestamp
                }
          ),
        `the runs over the first ${length} frames must be the runs over all of them, truncated`
      );
    }
  });
});

describe("replyRunAt", () => {
  const runs = buildReplyRuns([delta("a"), delta("b"), delta("c"), toolStart()]);

  it("finds the run from any frame inside it", () => {
    for (const index of [1, 2, 3]) {
      assert.equal(replyRunAt(runs, index)?.endIndex, 3, `index ${index} is inside the run`);
    }
  });

  it("answers null off the run", () => {
    assert.equal(replyRunAt(runs, 4), null, "the tool frame is not part of any reply");
    assert.equal(replyRunAt(runs, 0), null, "index 0 is before the first frame");
  });
});
