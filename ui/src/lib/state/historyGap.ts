/**
 * Where this view's history is discontinuous — the seam left when a stale frame
 * cache is spliced onto a window the server could only serve from later.
 *
 * The mechanism, end to end. On continue-as-new the workflow keeps only a
 * bounded tail of its stream log (`_truncate_stream_for_handover`, 512 KiB) and
 * carries `base_offset` with it, so offsets stay monotonic and a client polling
 * at the head resumes seamlessly. A client that was NOT at the head does not:
 * it hydrates its per-session `sessionStorage` cache, calls
 * `attach(lastResumeOffset)`, and `/api/attach` answers from the oldest offset
 * it still holds without saying that is what it did. Measured on a live
 * session: `from_offset=2000` returned a first event at `event_offset: 4906`,
 * and the console spliced 2,023 cached frames onto 2,044 new ones and drew
 * 4,067 events as though they were consecutive. The waterfall then labelled the
 * straddling turn "no measured parent steps" — true about having no spans,
 * silent about why, and indistinguishable from a turn that really ran none.
 *
 * Detected here, from the frames, rather than recorded as the stream stages
 * them: `event_offset` is durable and travels in the cache, so the frame list
 * IS the record and a second copy in controller state could only disagree with
 * it. Deriving also makes the answer right for a cursor mid-run for free — a
 * projection is handed the visible PREFIX, so a seam ahead of the playhead is
 * not yet claimed, and one behind it is.
 *
 * Not detected server-side, though `/api/attach` is better placed to know: it
 * has both numbers and would not have to infer anything. What that extra
 * knowledge buys is exactly the case that must NOT be flagged — `from_offset=0`
 * against a truncated log is the same server-side condition (`base_offset >
 * from_offset`) as the broken splice, and it is the documented, acceptable
 * degradation: a fresh tab gets a short scrollback with no hole inside it. A
 * server signal would therefore still need the client to decide whether it
 * straddles the seam, which is the comparison below, so the field would be
 * redundant rather than authoritative. Two frames the client HOLDS is also the
 * right definition on its own terms, because a discontinuity nobody is looking
 * across is not one a reader can be misled by.
 *
 * What that costs, honestly: a gap can only be seen from both sides of it. A
 * tab that cold-loads a truncated session is missing the head of the run and
 * nothing here says so — deliberately, per above. And the seam says only that
 * offsets jumped, never how many events fell in the hole, because the client
 * does not know: `event_offset` counts log entries, and how many turns, tools
 * or replies those entries amounted to died with them.
 */
import { SYNTHESIZED, type AgentSseFrame } from "$lib/api/types";
import { isRootAgentEvent } from "$lib/state/agentIdentity";

/**
 * The seam's note, in this codebase's mark for "we do not know" — the leading em
 * dash NO_THOUGHT_SUMMARY, UNKNOWN_TOOL_INPUT and formatCost all use. It states
 * the absence and the reason and counts nothing, since there is no count to give.
 */
export const HISTORY_GAP_NOTE =
  "— Events are missing here. The session's retained history had already been " +
  "trimmed past this point when this view resumed, so what follows is not " +
  "continuous with what precedes it.";

/** The shape both projections walk: a bare frame, or one tagged with its publisher. */
type GapInput = AgentSseFrame | { frame: AgentSseFrame };

/**
 * This frame's position in the ROOT agent's log, or null when it does not sit in
 * that sequence at all.
 *
 * Establishing which frames share one offset space is the whole difficulty, and
 * four kinds do not:
 *
 * - A SUBAGENT's events are offsets in that child's OWN log, so they interleave
 *   with the root's at unrelated values — a run with children would look like
 *   nothing but gaps. Asked with `isRootAgentEvent`, the same rule
 *   `buildReplayTimeline` decides `role` with, rather than a second opinion:
 *   `agent_id` is tree-unique and a child's carries a trailing fresh segment, so
 *   it answers even for a child nobody announced (its `subagent_started` is on
 *   the parent's log, which a stream opened past that offset never carries).
 * - A SYNTHESIZED frame is one the server made up rather than read off a log —
 *   the merge's `subagent_stream_unavailable` marker — so it has no durable
 *   coordinate and reports the sentinel. Confirmed live: those arrive as -1.
 * - A CLIENT-SIDE stream error carries no event envelope at all (no `type`), so
 *   there is no offset on it to read.
 * - A frame from a server predating `event_offset`, and every mock fixture,
 *   simply has no field. Absent is not zero.
 *
 * Deliberately NOT excluded: the session-level frames at `turn: 0`. They look
 * like they might sit outside the sequence and they do not — a live session was
 * observed publishing turns 0, 5, 6 and 7 into one dense run of offsets — so
 * skipping them the way the display projections do would manufacture a gap at
 * every one of them.
 */
function rootLogOffset(item: GapInput): number | null {
  const { data } = "frame" in item ? item.frame : item;
  if (!("type" in data)) return null;
  if (!isRootAgentEvent(data)) return null;
  const offset = data.event_offset;
  if (typeof offset !== "number" || offset === SYNTHESIZED) return null;
  return offset;
}

/**
 * The positions at which the run's history is discontinuous, as 0-based indices
 * into `input` — each one the frame that FOLLOWS a seam.
 *
 * A gap is a forward jump in the root log's offsets, because that log is dense:
 * every event the root publishes takes the next offset, and `/api/attach`
 * replays them all with no filter and no skip. Verified rather than assumed —
 * 5,411 root frames across four live sessions, spanning tool calls, approvals,
 * multiple turns and a session already truncated to `base_offset: 2329`, ran
 * +1 throughout with not one jump. (The one path that legitimately skips root
 * offsets is `send_message`'s `skip_until_turn_id` preamble behind
 * `POST /api/chat`, which this console does not use: it posts to
 * `/api/messages` and reads everything through `attach`.)
 *
 * Measured against the HIGHEST offset seen rather than the previous one, which is
 * what keeps a repeated or out-of-order frame from manufacturing a seam. Neither
 * should happen — frames dedupe on `agent_id|event_offset`, and a resume asks for
 * the offset just past the last root event seen — but tracking the last offset
 * would make one stray frame at 4 after a 5 report the following 6 as a jump,
 * inventing a hole out of two events that are both present. A high-water mark
 * cannot: it only ever asks whether this frame skipped past everything in hand.
 *
 * A seam needs a frame on BOTH sides: the first root frame in hand has nothing
 * to be discontinuous with, so a fresh tab that attaches from 0 to a truncated
 * stream reports nothing. That is the requirement not to cry wolf over the
 * ordinary post-rollover session, and it holds structurally here rather than by
 * a case that special-pleads it.
 */
export function findHistoryGaps(input: readonly GapInput[]): Set<number> {
  const gaps = new Set<number>();
  let highest: number | null = null;
  for (let position = 0; position < input.length; position += 1) {
    const offset = rootLogOffset(input[position]);
    if (offset == null) continue;
    if (highest != null && offset > highest + 1) gaps.add(position);
    highest = highest == null ? offset : Math.max(highest, offset);
  }
  return gaps;
}
