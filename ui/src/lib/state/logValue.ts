/**
 * How one field of a log row is turned into the text a reader sees.
 *
 * This was two byte-identical copies, one in TranscriptPanel.svelte and one in
 * AgentChatPanel.svelte, and both needed the same change on the same day. That is
 * the shape that drifts: inboundMessageText.ts already documents three divergent
 * copies of renderUserMessage() elsewhere in this codebase. One copy now, because
 * the wording below has to be identical in both panels and in the check that pins
 * it — not because a formatting layer was wanted. Nothing general belongs here.
 *
 * The distinction it exists to keep:
 *
 *   undefined -> ""   the row has no such field, so there is nothing to say
 *   null      -> the note below, because the field is present and its value is unknown
 *
 * Collapsing those two (`value == null`) is what this file fixes. `input` is the
 * only nullable field on a ReplayLogRow — body, detail and output are
 * `string | undefined` — so a null arriving here means exactly one thing, which is
 * why the note can be specific about tool arguments.
 */

/**
 * Shown in place of a tool call's arguments when the backend reported them as unknown.
 *
 * Leading em dash is this codebase's existing mark for "we do not know", the same one
 * formatCost() returns for a run it cannot price: an unreplayable run renders "—" rather
 * than a confident $0.0000. An unparseable argument buffer rendering as `{}` would be that
 * error one type over — a lost payload shown as a firm claim that the model passed nothing.
 */
export const UNKNOWN_TOOL_INPUT =
  "— The model streamed arguments, but the stream ended before they could be parsed.";

export function formatLogValue(value: unknown): string {
  if (value === undefined) return "";
  if (value === null) return UNKNOWN_TOOL_INPUT;
  if (typeof value === "string") return value.trim();
  return JSON.stringify(value, null, 2);
}
