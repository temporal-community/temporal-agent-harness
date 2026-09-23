/**
 * A streamed reply, as the one event a reader sees rather than the dozens of
 * frames it arrives as.
 *
 * `reply_delta` is published per chunk, so a paragraph of model output can be
 * sixty frames. Every one of them is a stop on the replay cursor and a row in
 * the log, which is most of a session spent scrubbing past the same sentence
 * appearing one clause at a time. This folds a consecutive run of them into one
 * unit; the frames themselves are untouched, and nothing here is persisted or
 * sent anywhere. It is a reading of the log, not a change to it.
 *
 * Runs are keyed by publishing agent and MESSAGE, not by turn: a refcounted turn
 * can hold several `mid_turn: "accept"` handlers streaming at once, and keying by
 * turn would fuse two participants' replies into one — the same trap transcript.ts
 * documents at replyIndexByMessage. Any frame that is not a `reply_delta` closes
 * the open run, so a tool call or a state commit between two chunks splits them,
 * and the log keeps saying that something happened in between.
 */
import type { AgentSseFrame } from "$lib/api/types";
import { messageKey } from "$lib/state/inboundMessageText";

export interface ReplyRun {
  /** Publishing agent plus message id — see the note above on why not turn id. */
  key: string;
  /** 1-based index of the run's first frame, aligned with AgentRunController.viewIndex. */
  startIndex: number;
  /** 1-based index of its last frame so far. A run still streaming ends at the last one seen. */
  endIndex: number;
  /** Every chunk's text, in arrival order. */
  text: string;
  startTs: number;
  endTs: number;
  frameCount: number;
}

interface ReplyRunEntry {
  frame: AgentSseFrame;
  workflowId?: string;
}

/**
 * Fold consecutive reply chunks into runs, in order.
 *
 * Takes the same shape the other projections do — a bare frame, or one tagged
 * with the agent that published it — so the log and the transport can share this
 * derivation rather than each carrying the rule.
 */
export function buildReplyRuns(
  input: readonly (AgentSseFrame | ReplyRunEntry)[]
): ReplyRun[] {
  const runs: ReplyRun[] = [];
  let open: ReplyRun | null = null;

  input.forEach((item, position) => {
    const entry: ReplyRunEntry = "frame" in item ? item : { frame: item };
    const { frame } = entry;
    if (frame.event !== "reply_delta" || !("type" in frame.data)) {
      open = null;
      return;
    }

    const key = `${entry.workflowId ?? frame.data.agent_id}:${messageKey(frame)}`;
    const index = position + 1;
    const { text, timestamp } = frame.data;

    /* Contiguity needs no check of its own: every non-delta frame above clears
       `open`, so an open run's last frame is always the one before this. */
    if (open != null && open.key === key) {
      open.endIndex = index;
      open.endTs = timestamp;
      open.text += text;
      open.frameCount += 1;
      return;
    }

    open = {
      key,
      startIndex: index,
      endIndex: index,
      text,
      startTs: timestamp,
      endTs: timestamp,
      frameCount: 1
    };
    runs.push(open);
  });

  return runs;
}

/** The run a 1-based frame index falls inside, or null when it is not a reply chunk. */
export function replyRunAt(runs: readonly ReplyRun[], index: number): ReplyRun | null {
  return (
    runs.find((run) => index >= run.startIndex && index <= run.endIndex) ?? null
  );
}
