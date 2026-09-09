// ABOUTME: Asserts the frame identity #ingestFrame dedupes on, imported from
// agentRun.svelte.ts rather than copied. The properties that matter: a redelivered event keys the
// same (reconnects overlap), two distinct events of one agent never key the same (or the second is
// silently dropped), and the frames with no offset to report still fall back to the old payload hash
// rather than colliding on -1.
//   npx vitest run src/lib/state/frameKey.test.mjs
//
// This test used to carry a hand-copied twin of frameKey, kept "in step" by hand. That is the one
// failure a dedupe check cannot afford: the copy and its assertions agree with each other while the
// shipped function drifts away from both, so the check passes brightest exactly when it is wrong.
//
// Imported rather than copied, which vitest makes free: it runs the file through vite, so a
// rune-using module compiles on the way in. Plain node gets further than you would expect — it
// strips the types unaided — but stops on the extensionless relative imports (`./bootSession`),
// and a rune module needs compileModule() rather than the compile() that serves a component.
// Both were resolvers of our own to maintain until vite owned the job.
import assert from "node:assert/strict";
import { describe, it } from "vitest";

/* From the same place the shipped function reads it, so the sentinel cannot drift either. */
import { SYNTHESIZED } from "../api/types.ts";
import { frameKey } from "./agentRun.svelte.ts";

const ev = (over = {}) => ({
  event: "reply_delta",
  data: {
    type: "reply_delta",
    agent_id: "root",
    turn_id: "t1",
    turn_number: 1,
    timestamp: 1.0,
    resume_offset: 5,
    event_offset: 4,
    delta: "hi",
    ...over,
  },
});

describe("frameKey", () => {
  it("identifies an event by its own offset, never by the resume cursor", () => {
    // A redelivered event keys the same even though the resume cursor moved on, which is the whole
    // point: a reconnect replays events the client already has.
    assert.equal(
      frameKey(ev()),
      frameKey(ev({ resume_offset: 99 })),
      "the same event must key identically regardless of the resume cursor"
    );

    // Two events of one agent are distinct even with byte-identical bodies. Under the old payload hash
    // these collided and the second was dropped.
    assert.notEqual(
      frameKey(ev({ event_offset: 4 })),
      frameKey(ev({ event_offset: 5 })),
      "two events of one agent must never share a key"
    );

    // The same offset in two different agents' logs is two different events.
    assert.notEqual(
      frameKey(ev({ agent_id: "root" })),
      frameKey(ev({ agent_id: "root-child" })),
      "an offset is only meaningful within one agent's log"
    );

    // Identical deltas at different offsets stay distinct — the truncated-reply failure mode.
    const deltas = ["  ", "  ", "  ", "  "].map((delta, i) =>
      frameKey(ev({ delta, event_offset: i }))
    );
    assert.equal(
      new Set(deltas).size,
      4,
      "repeated delta text must not collapse into one frame"
    );
  });

  it("does not collide the frames that have no offset to report", () => {
    // Synthesized frames have no offset, so they fall back to the payload hash rather than all keying
    // on -1, which would make one marker per child the most a session could ever show.
    const marker = (subagentId) => ({
      event: "subagent_stream_unavailable",
      data: {
        type: "subagent_stream_unavailable",
        agent_id: subagentId,
        turn_id: "",
        turn_number: 0,
        timestamp: 0.0,
        resume_offset: 7,
        event_offset: SYNTHESIZED,
        subagent_id: subagentId,
        workflow_id: `wf-${subagentId}`,
        reason: "gone",
      },
    });
    assert.notEqual(
      frameKey(marker("child-a")),
      frameKey(marker("child-b")),
      "two children's markers must not collide on SYNTHESIZED"
    );
  });

  it("still keys a frame carrying no envelope at all", () => {
    // A frame from a server predating event_offset falls back rather than throwing.
    const legacy = ev();
    delete legacy.data.event_offset;
    assert.equal(typeof frameKey(legacy), "string");
    assert.ok(
      frameKey(legacy).startsWith("reply_delta|"),
      "a frame with no event_offset must take the payload-hash path"
    );

    // A client-side stream error carries no envelope at all and must still key.
    const streamError = {
      event: "error",
      data: { kind: "timeout", message: "nope", resume_offset: 3 },
    };
    assert.equal(typeof frameKey(streamError), "string");
    assert.equal(
      frameKey(streamError),
      frameKey({ ...streamError, data: { ...streamError.data, resume_offset: 8 } }),
      "an envelope-less error must key on its content, not the resume cursor"
    );
  });

  it("costs less to hold than the payload hash it replaced", () => {
    // The cost of the two identities, since dedupe holds every key for the life of the session.
    const real = ev({ delta: "x".repeat(180) });
    const oldKey = `${real.event}|${JSON.stringify({
      ...real.data,
      resume_offset: undefined,
    })}`;
    assert.ok(
      frameKey(real).length < oldKey.length,
      `the offset identity must be the cheaper one to hold (offset=${frameKey(real).length}B, payload-hash=${oldKey.length}B)`
    );
  });
});
