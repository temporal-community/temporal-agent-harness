/**
 * The no-modal fallback: fill a handler's fields from one line of text.
 *
 * Not every platform can render a form. The Discord adapter ships no `openModal` at all, and
 * several platforms have neither modals nor slash commands. Rather than leave those agents
 * reachable only through free-text chat — which would silently hide every typed handler — we
 * define a grammar the server parses and a template it can show the user.
 *
 * It deliberately produces `Record<string, string>`, the same shape a modal submission
 * produces, so `decodeModalValues` does the coercion and validation for both paths. There is
 * one place where a field's type is restored, not two.
 */

import type { FieldPlan } from './schema-to-modal.ts';

/**
 * Split `a=1 b="two words" c='three'` into tokens, honouring quotes.
 *
 * Hand-rolled rather than regex-split because values routinely contain the `=` and spaces
 * that a naive split would destroy.
 */
function tokenize(text: string): string[] {
  const tokens: string[] = [];
  let current = '';
  let quote: string | undefined;

  for (const char of text) {
    if (quote) {
      if (char === quote) quote = undefined;
      else current += char;
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      continue;
    }
    if (/\s/.test(char)) {
      if (current) tokens.push(current);
      current = '';
      continue;
    }
    current += char;
  }
  if (current) tokens.push(current);
  return tokens;
}

/** True when the whole argument text should be taken as one field's value. */
function isSingleProseField(fields: FieldPlan[]): boolean {
  const [only] = fields;
  return fields.length === 1 && !!only && (only.kind === 'text' || only.kind === 'string');
}

/**
 * Parse argument text into raw field values.
 *
 * Two shapes, because one grammar cannot serve both well:
 *
 *   - A handler with a single string field — `ask(text)`, the common case — takes the entire
 *     remaining text verbatim. Requiring `text="..."` there would be hostile.
 *   - Anything else takes `key=value` pairs in any order.
 */
export function parseFieldText(fields: FieldPlan[], text: string): Record<string, string> {
  const trimmed = text.trim();
  if (trimmed === '') return {};

  if (isSingleProseField(fields)) {
    const only = fields[0]!;
    // Still accept the explicit form, so the template we print always works.
    const explicit = new RegExp(`^${only.name}=`);
    if (!explicit.test(trimmed)) return { [only.name]: trimmed };
  }

  const values: Record<string, string> = {};
  for (const token of tokenize(trimmed)) {
    const eq = token.indexOf('=');
    if (eq <= 0) continue;
    values[token.slice(0, eq)] = token.slice(eq + 1);
  }
  return values;
}

/** A short type hint for the usage line. */
function hint(field: FieldPlan): string {
  switch (field.kind) {
    case 'enum':
      return (field.options ?? []).join('|');
    case 'boolean':
      return 'true|false';
    case 'integer':
    case 'number':
      return 'number';
    case 'date':
      return 'YYYY-MM-DD';
    case 'json':
      return 'json';
    default:
      return 'text';
  }
}

/**
 * A copy-pasteable usage line plus one line per field.
 *
 * Addresses the handler directly (`/set_approval_policy ...`), because that is what a user
 * actually types: a leading slash in an ordinary message routes to the handler, and no
 * platform slash command needs to exist for it to work.
 *
 * This is what stands in for the modal on a platform that has none, so it has to carry
 * everything the form would have shown: which fields exist, which are required, what values
 * are allowed. Optional fields are bracketed.
 */
export function fieldsToTemplate(handlerName: string, fields: FieldPlan[]): string {
  const command = `/${handlerName}`;
  if (fields.length === 0) return `\`${command}\``;

  if (isSingleProseField(fields)) {
    const only = fields[0]!;
    return [
      `\`${command} <${only.name}>\``,
      only.description ? `• ${only.name} — ${only.description}` : `• ${only.name} — text`,
    ].join('\n');
  }

  const usage = fields
    .map((f) => (f.required ? `${f.name}=<${hint(f)}>` : `[${f.name}=<${hint(f)}>]`))
    .join(' ');

  const lines = fields.map((f) => {
    const parts = [`• ${f.name} (${hint(f)}${f.required ? '' : ', optional'})`];
    if (f.description) parts.push(`— ${f.description}`);
    // A field that had to fall back to JSON is the one a user is most likely to get wrong.
    if (f.jsonFallbackReason) parts.push(`— pass JSON: ${f.jsonFallbackReason}`);
    return parts.join(' ');
  });

  return [`\`${command} ${usage}\``, ...lines].join('\n');
}
