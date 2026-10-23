// A best-effort reading of a JSON document cut off mid-stream, so a reply card can fill in while
// the model is still writing it.

/** Open strings and brackets in `text`: whether it ends inside a string (and after a backslash),
 *  and the closers still owed, innermost first. */
function scan(text: string): { inString: boolean; escaped: boolean; closers: string[] } {
  const stack: string[] = [];
  let inString = false;
  let escaped = false;
  for (const ch of text) {
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
    } else if (ch === '"') inString = true;
    else if (ch === "{") stack.push("}");
    else if (ch === "[") stack.push("]");
    else if (ch === "}" || ch === "]") stack.pop();
  }
  return { inString, escaped, closers: stack.reverse() };
}

/**
 * Parse a prefix of a JSON document: close an open string and the open brackets, and drop a
 * dangling key, comma or half-written literal. Returns undefined until something parses.
 *
 * Best effort: the last string in an array is held back until the next character arrives. On
 * text that never parses it retries every shorter prefix, which is quadratic, so call it once
 * per flush, and only for replies a card is registered for.
 */
export function parsePartialJson(text: string): unknown {
  const open = scan(text);
  const body = open.inString ? text + (open.escaped ? '\\"' : '"') : text;
  for (let cut = body.length; cut > 0; cut--) {
    const head = body
      .slice(0, cut)
      .replace(/[\s,:]+$/, "")
      .replace(/,\s*"[^"]*"$/, "")
      .replace(/\{\s*"[^"]*"$/, "{");
    const { inString, closers } = scan(head);
    if (inString) continue;
    try {
      return JSON.parse(head + closers.join(""));
    } catch {
      /* not yet a document: try a shorter prefix */
    }
  }
  return undefined;
}
