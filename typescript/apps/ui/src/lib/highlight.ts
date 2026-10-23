import { createHighlighter } from "@tanstack/highlight/core";
import {
  cpp,
  css,
  diff,
  dockerfile,
  env,
  go,
  html,
  java,
  js,
  json,
  jsx,
  markdown,
  python,
  ruby,
  rust,
  shell,
  sql,
  svelte,
  toml,
  ts,
  tsx,
  yaml
} from "@tanstack/highlight/languages";

/** Past either, code is drawn as plain text: highlighting a megabyte is tens of thousands of nodes. */
export const HIGHLIGHT_LIMIT = { lines: 2_000, chars: 200_000 } as const;

/* Only the tokenizer: its tokens carry raw text, so the escaping and the class names stay ours.
   The package has no C grammar; its C++ one reads C. */
const highlighter = createHighlighter({
  languages: [
    { ...cpp, aliases: [...(cpp.aliases ?? []), "c", "h"] },
    css,
    diff,
    dockerfile,
    env,
    go,
    html,
    java,
    js,
    json,
    jsx,
    markdown,
    python,
    ruby,
    rust,
    shell,
    sql,
    svelte,
    toml,
    ts,
    tsx,
    yaml
  ]
});

const ENTITIES: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ENTITIES[char] ?? char);
}

function overLimit(code: string): boolean {
  if (code.length > HIGHLIGHT_LIMIT.chars) return true;
  let lines = 1;
  for (let at = code.indexOf("\n"); at !== -1; at = code.indexOf("\n", at + 1)) {
    if (++lines > HIGHLIGHT_LIMIT.lines) return true;
  }
  return false;
}

/** `code` as escaped HTML with `md-syntax-*` spans; plain in an unknown language or past the limit. */
export function highlight(code: string, language: string): string {
  if (overLimit(code)) return escapeHtml(code);
  let html = "";
  for (const { className, value } of highlighter.tokenize(code, { lang: language }).tokens) {
    html += className ? `<span class="md-syntax-${className}">${escapeHtml(value)}</span>` : escapeHtml(value);
  }
  return html;
}
