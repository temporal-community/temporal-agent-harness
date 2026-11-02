import type { JsonValue } from "$lib/api/types";

/**
 * A tool's result, which `tool_end` carries as JSON, as the text the console shows: a string as
 * it is, anything else as indented JSON. Read once where a frame is taken in, so every view (the
 * log, the flow's result, image and script detection, search) works on the same text.
 */
export function toolOutputText(output: JsonValue): string {
  return typeof output === "string" ? output : JSON.stringify(output, null, 2);
}
