/**
 * How a `thought_summary` frame's `delta` becomes text a reader sees.
 *
 * The event carries whatever the provider dumped — `ThoughtSummaryDelta.delta` is a bare
 * `dict[str, Any]` by design, "dumped raw so a consumer can position it" — so the shape is
 * the provider's, not the harness's, and the three producers in ai_sdks/ dump three:
 *
 *   Gemini      DeltaThoughtSummary   { content: { type: "text", text } }
 *   OpenAI      ResponseReasoningSummaryTextDeltaEvent  { delta, item_id, … }
 *   Pydantic AI ThinkingPart          { content, part_kind: "thinking" }
 *               ThinkingPartDelta     { content_delta, part_delta_kind: "thinking_delta" }
 *
 * Reading only the first of those is what rendered an empty thought card for every OpenAI
 * run: the text was in `delta.delta` and nothing looked there. Pydantic AI failed the same
 * way one step earlier — its `content` is a string, so an `is object` guard on the Gemini
 * shape skipped it too.
 *
 * Every one of these is a FRAGMENT, not a whole thought: OpenAI and Pydantic AI stream
 * token-sized deltas, and Gemini's own docstring calls its payload "a new summary item to
 * be added to the thought". A caller showing one thought has to accumulate them, exactly as
 * it already does for `reply_delta`.
 */

import type { JsonRecord } from "$lib/api/types";

/**
 * Shown on a thought card whose frames carried no text — a signature-only Pydantic AI delta,
 * or a payload shape newer than the extractor above.
 *
 * It exists because that state rendered as a blank card, which is exactly how the OpenAI bug
 * rendered, and two different states with one appearance is most of why the bug sat. Leading
 * em dash is this codebase's mark for "we do not know", the same one UNKNOWN_TOOL_INPUT and
 * formatCost use.
 */
export const NO_THOUGHT_SUMMARY =
  "— The model reported thinking, but no thought summary text was streamed.";

const asText = (value: unknown): string => (typeof value === "string" ? value : "");

/** One fragment of thought text, or "" for a frame that carries none (a signature delta). */
export function thoughtDeltaText(delta: JsonRecord): string {
  const content = delta.content;
  /* Gemini's content is TextContent | ImageContent — an image has no text and reads as "". */
  if (typeof content === "object" && content != null && "text" in content) {
    return asText((content as { text?: unknown }).text);
  }
  return asText(content) || asText(delta.content_delta) || asText(delta.delta);
}
